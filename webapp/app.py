"""
webapp/app.py - Local web UI for DrawMeBinary.

A thin Flask wrapper around the existing pipeline. It adds NO decoding logic
of its own: it validates the upload with drawmebinary.security, runs
drawmebinary.pipeline, renders both modes, and returns the results as inline
images plus the decoded text.

Privacy / safety:
  * Uploads are capped at 5 MB and validated by magic bytes (the same guards
    as the CLI): disguised executables, oversized images and bombs are
    refused.
  * Nothing is written to a persistent location. Each request works in a
    fresh temp directory that is deleted before the response returns; the
    only "save" is the user clicking a download button in the browser.
  * The decoded message is untrusted. It is sent to the browser as text and
    inserted with textContent (never innerHTML) and is never executed. If it
    looks like code, the UI shows a warning.

Security logging:
  * Every request and every security-relevant outcome is emitted as an ECS
    JSON event via drawmebinary.seclog (see SECURITY_LOGGING.md), ready for
    a SIEM. Decoded content is never logged - only a SHA-256 of it.
  * A small in-memory per-IP rate limiter emits rate_limit.exceeded events.

Run:  python webapp/app.py   then open http://127.0.0.1:5000
"""
import base64
import os
import sys
import time
import tempfile
from collections import deque

from flask import Flask, g, jsonify, render_template, request

# make the pipeline package importable when run from the project root
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'drawmebinary'))

from config import CONFIG, PRESETS, get_config_for_preset   # noqa: E402
from decoding import LanguageValidator                       # noqa: E402
from pipeline import run_pipeline                            # noqa: E402
from rendering import render_basic_mode, render_poster_mode  # noqa: E402
from security import SecurityError, validate_input_file, sanitize_text  # noqa: E402
import seclog                                                # noqa: E402

MAX_MB = 5
ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}

# GDPR: log only the browser family, not the full User-Agent string.
# Full UAs reveal OS version + device type and are personal data when
# combined with a (even truncated) IP and timestamp.
_UA_RE = __import__('re').compile(
    r'(Chrome|Firefox|Safari|Edg|Edge|curl|python-requests|wget|bot|spider)'
    r'/[\d.]+', __import__('re').IGNORECASE)


def _ua_family(ua: str) -> str:
    """Return 'Chrome/121', 'Firefox/120', 'curl/7' etc., or 'other'."""
    m = _UA_RE.search(ua or '')
    return m.group(0) if m else 'other'

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_MB * 1024 * 1024

# web-app config: 5 MB cap, logs tagged as the web service
_CONF = dict(CONFIG)
_CONF['max_input_mb'] = MAX_MB
_CONF['log_service_name'] = 'drawmebinary-web'
_LOG = seclog.get_logger(_CONF)
_VALIDATOR = LanguageValidator()

# --- minimal in-memory per-IP rate limiter (sliding window) ----------------
_RL_HITS = {}            # ip -> deque[timestamps]
_RL_MAX = _CONF.get('rate_limit_max', 30)
_RL_WIN = _CONF.get('rate_limit_window_s', 60)
_RL_ON = _CONF.get('rate_limit_enabled', True)


def _client_ip() -> str:
    # honour a single proxy hop if present (configure your proxy to set this)
    xff = request.headers.get('X-Forwarded-For', '')
    return (xff.split(',')[0].strip() if xff else request.remote_addr) or ''


def _rate_limited(ip: str) -> bool:
    if not _RL_ON:
        return False
    now = time.monotonic()
    dq = _RL_HITS.setdefault(ip, deque())
    while dq and now - dq[0] > _RL_WIN:
        dq.popleft()
    if len(dq) >= _RL_MAX:
        return True
    dq.append(now)
    return False


def _png_data_uri(path: str) -> str:
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('ascii')
    return f'data:image/png;base64,{b64}'


@app.before_request
def _begin():
    g._cid = seclog.new_correlation()
    g._t0 = time.perf_counter()
    g._ip = _client_ip()
    _LOG.event('request.received', category='web', outcome='unknown',
               event_type='access', message=f'{request.method} {request.path}',
               source_ip=g._ip, http_method=request.method,
               url_path=request.path,
               user_agent=_ua_family(request.headers.get('User-Agent', '')))


@app.after_request
def _finish(resp):
    seclog.bind_correlation(getattr(g, '_cid', None))
    dur = round((time.perf_counter() - getattr(g, '_t0', time.perf_counter()))
                * 1000, 1)
    _LOG.event('request.completed', category='web',
               outcome='success' if resp.status_code < 400 else 'failure',
               event_type='access',
               source_ip=getattr(g, '_ip', None), http_method=request.method,
               url_path=request.path, status_code=resp.status_code,
               duration_ms=dur)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
    return resp


@app.route('/')
def index():
    return render_template('index.html', max_mb=MAX_MB,
                           presets=list(PRESETS.keys()))


@app.route('/decode', methods=['POST'])
def decode():
    ip = getattr(g, '_ip', _client_ip())
    if _rate_limited(ip):
        _LOG.event('rate_limit.exceeded', category='network',
                   outcome='failure', level='warning',
                   event_type='denied', message='rate limit exceeded',
                   source_ip=ip, url_path='/decode',
                   limit=f'{_RL_MAX}/{_RL_WIN}s')
        return jsonify(error='Too many requests; please slow down.'), 429

    f = request.files.get('image')
    if f is None or not f.filename:
        return jsonify(error='No file uploaded.'), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        _LOG.event('validation.refused', category='file', outcome='failure',
                   level='warning', event_type='denied',
                   message='disallowed extension', source_ip=ip,
                   reason='extension not allowed', declared_type=ext)
        return jsonify(error=f'Unsupported type {ext or "?"}. Allowed: '
                       + ', '.join(sorted(ALLOWED_EXT))), 400

    preset = request.form.get('preset') or None
    try:
        config = get_config_for_preset(preset) if preset else dict(CONFIG)
    except KeyError:
        config = dict(CONFIG)
    config['max_input_mb'] = MAX_MB

    with tempfile.TemporaryDirectory(prefix='dmb_web_') as tmp:
        in_path = os.path.join(tmp, 'upload' + ext)
        f.save(in_path)
        config['output_dir'] = tmp
        try:
            file_bytes = os.path.getsize(in_path)
            report = validate_input_file(in_path, config)
            result = run_pipeline(in_path, config, _VALIDATOR, verbose=False)

            sec = result.get('security', {})
            with open(in_path, 'rb') as fh:
                fhash = seclog.hash_bytes(fh.read())
            _LOG.event('decode.completed', category='file',
                       outcome='success', event_type='allowed',
                       message='image decoded', source_ip=ip,
                       file_size=file_bytes, detected_type=report.get('kind'),
                       declared_type=ext, file_hash=fhash,
                       image_w=result.get('orig_size', (0, 0))[0],
                       image_h=result.get('orig_size', (0, 0))[1],
                       n_streams=len(result.get('streams', [])),
                       content_len=len(result.get('text', '')),
                       content_hash=seclog.hash_text(result.get('text', '')))
            if sec.get('code_suspect'):
                _LOG.event('code.detected', category='intrusion_detection',
                           outcome='success', level='warning',
                           event_type='indicator',
                           message='decoded content looks like code',
                           source_ip=ip, findings=sec.get('findings', []),
                           content_hash=seclog.hash_text(
                               result.get('text', '')))

            basic_path = render_basic_mode(result, config, in_path,
                                           img=result.get('img'))
            poster_path = render_poster_mode(result, config, _VALIDATOR,
                                             in_path, img=result.get('img'))
            payload = {
                'text': sanitize_text(result['text']),
                'security': sec,
                'basic': _png_data_uri(basic_path),
                'poster': _png_data_uri(poster_path),
            }
        except SecurityError as exc:
            _LOG.event('validation.refused', category='file',
                       outcome='failure', level='warning',
                       event_type='denied', message=str(exc), source_ip=ip,
                       reason=str(exc), declared_type=ext)
            return jsonify(error=f'Refused: {exc}'), 400
        except Exception as exc:                       # pragma: no cover
            _LOG.event('request.error', category='web', outcome='failure',
                       level='error', event_type='error',
                       message='unhandled error', source_ip=ip,
                       error_type=type(exc).__name__)
            return jsonify(error='Could not process image.'), 500
    return jsonify(payload)


@app.errorhandler(413)
def _too_large(_e):
    _LOG.event('upload.too_large', category='file', outcome='failure',
               level='warning', event_type='denied',
               message='upload exceeds size cap',
               source_ip=getattr(g, '_ip', None), limit=f'{MAX_MB}MB')
    return jsonify(error=f'File too large (limit {MAX_MB} MB).'), 413


if __name__ == '__main__':
    _LOG.event('app.started', category='process', outcome='success',
               message='web app starting', url_path='/', limit=f'{MAX_MB}MB')
    print(f'DrawMeBinary web UI -> http://127.0.0.1:5000  (limit {MAX_MB} MB)')
    app.run(host='127.0.0.1', port=5000, debug=False)
