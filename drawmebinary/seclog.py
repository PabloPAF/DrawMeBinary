"""
seclog.py - Standardized security logging for DrawMeBinary (and future apps).

Emits one JSON object per line in the Elastic Common Schema (ECS) field
layout, so any ECS-aware SIEM (OpenSearch, Elastic, Wazuh, Grafana) can
ingest it with no custom parsing. The module is deliberately dependency-free
(stdlib only) and app-agnostic: point another service at it, set a different
`service.name`, and its logs land in the same schema and dashboards.

Design rules
------------
* One event per line, UTC `@timestamp`, stable event taxonomy (see EVENTS).
* A per-request correlation id (ECS `trace.id`) ties all events of one
  request together; set it with `new_correlation()` / `bind_correlation()`.
* Never log secrets or untrusted decoded content. `log_event` scrubs a
  denylist of field names, and callers pass a HASH of decoded text, never
  the text itself.
* Source IPs are personal data: `process_ip` stores them full, truncated
  (host bits zeroed) or salted-hashed, per config.

Taxonomy (event.action) used across the app:
    app.started, app.stopped,
    request.received, request.completed, request.error,
    validation.refused, upload.too_large, rate_limit.exceeded,
    decode.completed, code.detected
"""
import base64
import contextvars
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

ECS_VERSION = '8.11.0'

# field names that must never appear in a log line, whatever the caller does
_REDACT = {
    'text', 'decoded', 'decoded_text', 'content', 'message_text', 'payload',
    'password', 'passwd', 'secret', 'token', 'authorization', 'cookie',
    'api_key', 'apikey', 'session',
}

_correlation: contextvars.ContextVar[Optional[str]] = \
    contextvars.ContextVar('correlation_id', default=None)


def new_correlation() -> str:
    """Start a fresh correlation id for the current context and return it."""
    cid = uuid.uuid4().hex
    _correlation.set(cid)
    return cid


def bind_correlation(cid: Optional[str]) -> None:
    _correlation.set(cid)


def current_correlation() -> Optional[str]:
    return _correlation.get()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def process_ip(ip: Optional[str], mode: str = 'truncate',
               salt: str = '') -> Optional[str]:
    """Privacy-preserving rendering of a source IP for logs."""
    if not ip:
        return None
    if mode == 'full':
        return ip
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if mode == 'hash':
        digest = hashlib.sha256((salt + ip).encode()).hexdigest()[:16]
        return f'sha256:{digest}'
    # truncate: zero the host portion (last octet for v4, last 80 bits for v6)
    if addr.version == 4:
        net = ipaddress.ip_network(f'{ip}/24', strict=False)
    else:
        net = ipaddress.ip_network(f'{ip}/48', strict=False)
    return str(net.network_address)


def hash_bytes(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return hash_bytes(text.encode('utf-8', 'replace'))


def _set(dst: Dict[str, Any], dotted: str, value: Any) -> None:
    """Assign a nested ECS field from a dotted key, e.g. 'http.request.method'."""
    if value is None:
        return
    parts = dotted.split('.')
    node = dst
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):           # collision guard
            return
    node[parts[-1]] = value


def _scrub(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in fields.items()
            if k.split('.')[-1].lower() not in _REDACT}


# --------------------------------------------------------------------------
# Grafana Cloud Loki handler (optional — activated by env vars)
# --------------------------------------------------------------------------
class _LokiHandler(logging.Handler):
    """
    Ships each log record to Grafana Cloud Loki via the push HTTP API.

    Retention: Grafana Cloud free tier retains logs for 14 days, which is
    the documented retention period for this service. Set a shorter index
    lifecycle policy in Grafana if a shorter period is required.

    Activated when all three env vars are present:
        LOKI_URL   – push endpoint, e.g. https://logs-prod-012.grafana.net/loki/api/v1/push
        LOKI_USER  – numeric user ID shown on the Grafana Cloud Loki page
        LOKI_TOKEN – a Grafana Cloud API token with MetricsPublisher role

    Runs a background daemon thread + bounded queue so log shipping never
    blocks a request. Fails silently on any network error.
    """

    def __init__(self, url: str, user: str, token: str,
                 labels: Dict[str, str]) -> None:
        super().__init__()
        creds = base64.b64encode(f'{user}:{token}'.encode()).decode()
        self._url = url
        self._auth = f'Basic {creds}'
        self._stream_labels = labels
        self._q: queue.Queue = queue.Queue(maxsize=500)
        t = threading.Thread(target=self._worker, daemon=True,
                             name='loki-shipper')
        t.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait((time.time_ns(), record.getMessage()))
        except queue.Full:
            pass  # drop rather than block

    def _worker(self) -> None:
        while True:
            try:
                ts_ns, line = self._q.get(timeout=5)
            except queue.Empty:
                continue
            payload = json.dumps({
                'streams': [{
                    'stream': self._stream_labels,
                    'values': [[str(ts_ns), line]],
                }]
            }).encode()
            req = urllib.request.Request(
                self._url,
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': self._auth,
                },
                method='POST',
            )
            try:
                urllib.request.urlopen(req, timeout=5)
            except Exception as _e:
                print(f'[seclog] Loki ship error: {_e}', flush=True)


# --------------------------------------------------------------------------
# logger
# --------------------------------------------------------------------------
class SecurityLogger:
    """ECS JSON logger writing to stdout and/or a rotating file."""

    def __init__(self, config: Dict[str, Any]):
        self.service = config.get('log_service_name', 'app')
        self.version = config.get('log_service_version', '0.0.0')
        self.env = config.get('log_environment', 'development')
        self.host = socket.gethostname()
        self.ip_mode = config.get('log_ip_mode', 'truncate')
        self.ip_salt = config.get('log_ip_salt', '')

        self._logger = logging.getLogger(f'seclog.{self.service}')
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            fmt = logging.Formatter('%(message)s')   # we pre-serialize JSON
            if config.get('log_to_stdout', True):
                h = logging.StreamHandler(sys.stdout)
                h.setFormatter(fmt)
                self._logger.addHandler(h)
            if config.get('log_to_file', True):
                log_dir = config.get('log_dir', 'logs')
                try:
                    os.makedirs(log_dir, exist_ok=True)
                    fh = logging.handlers.RotatingFileHandler(
                        os.path.join(log_dir, 'security.jsonl'),
                        maxBytes=config.get('log_file_max_mb', 20) * 1_048_576,
                        backupCount=config.get('log_file_backups', 5),
                        encoding='utf-8')
                    fh.setFormatter(fmt)
                    self._logger.addHandler(fh)
                except OSError:
                    pass                              # stdout still works

            # Grafana Cloud Loki – active only when env vars are set
            loki_url = os.environ.get('LOKI_URL', '').strip()
            loki_user = os.environ.get('LOKI_USER', '').strip()
            loki_token = os.environ.get('LOKI_TOKEN', '').strip()
            if loki_url and loki_user and loki_token:
                lh = _LokiHandler(
                    url=loki_url,
                    user=loki_user,
                    token=loki_token,
                    labels={
                        'service': self.service,
                        'env': self.env,
                        'host': self.host,
                    },
                )
                self._logger.addHandler(lh)
                print(f'[seclog] Loki handler active -> {loki_url}',
                      flush=True)
            else:
                print(f'[seclog] Loki handler NOT active '
                      f'(LOKI_URL={bool(loki_url)} '
                      f'LOKI_USER={bool(loki_user)} '
                      f'LOKI_TOKEN={bool(loki_token)})',
                      flush=True)

    def event(self, action: str, category: str, outcome: str = 'success',
              message: str = '', level: str = 'info',
              event_type: str = 'info', **fields: Any) -> Dict[str, Any]:
        """
        Emit one ECS event.
          action    - taxonomy verb, e.g. 'validation.refused'
          category  - ECS event.category, e.g. 'network' | 'file' |
                      'intrusion_detection' | 'web' | 'process'
          outcome   - 'success' | 'failure' | 'unknown'
          fields    - dotted ECS keys, e.g. source_ip=..., http_method=...
                      (see _MAP for the friendly -> ECS aliases)
        """
        rec: Dict[str, Any] = {}
        rec['@timestamp'] = datetime.now(timezone.utc).isoformat()
        _set(rec, 'ecs.version', ECS_VERSION)
        _set(rec, 'log.level', level)
        if message:
            rec['message'] = message
        _set(rec, 'event.kind', 'event')
        _set(rec, 'event.category', [category])
        _set(rec, 'event.type', [event_type])
        _set(rec, 'event.action', action)
        _set(rec, 'event.outcome', outcome)
        _set(rec, 'service.name', self.service)
        _set(rec, 'service.version', self.version)
        _set(rec, 'service.environment', self.env)
        _set(rec, 'host.name', self.host)
        cid = current_correlation()
        if cid:
            _set(rec, 'trace.id', cid)

        for key, val in _scrub(fields).items():
            ecs_key = _MAP.get(key, key)
            if key in ('source_ip', 'client_ip'):
                val = process_ip(val, self.ip_mode, self.ip_salt)
            _set(rec, ecs_key, val)

        line = json.dumps(rec, ensure_ascii=False, default=str)
        getattr(self._logger, level if level in
                ('debug', 'info', 'warning', 'error', 'critical')
                else 'info')(line)
        return rec


# friendly kwarg -> ECS dotted field
_MAP = {
    'source_ip': 'source.ip',
    'client_ip': 'client.ip',
    'http_method': 'http.request.method',
    'url_path': 'url.path',
    'status_code': 'http.response.status_code',
    'user_agent': 'user_agent.original',
    'duration_ms': 'event.duration_ms',
    'file_size': 'file.size',
    'file_mime': 'file.mime_type',
    'file_hash': 'file.hash.sha256',
    'declared_type': 'labels.declared_type',
    'detected_type': 'labels.detected_type',
    'reason': 'labels.reason',
    'findings': 'labels.code_findings',
    'content_hash': 'labels.content_sha256',
    'content_len': 'labels.content_length',
    'n_streams': 'labels.streams',
    'image_w': 'labels.image_width',
    'image_h': 'labels.image_height',
    'rule': 'labels.rule',
    'limit': 'labels.limit',
    'error_type': 'error.type',
}


_INSTANCE: Optional[SecurityLogger] = None


def get_logger(config: Dict[str, Any]) -> SecurityLogger:
    """Process-wide singleton, built from config on first use."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SecurityLogger(config)
    return _INSTANCE


def reset_logger() -> None:
    """Drop the singleton (used by tests)."""
    global _INSTANCE
    if _INSTANCE is not None:
        for h in list(_INSTANCE._logger.handlers):
            _INSTANCE._logger.removeHandler(h)
            h.close()
    _INSTANCE = None
