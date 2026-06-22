# Security logging

DrawMeBinary emits structured, SIEM-ready security logs through
`drawmebinary/seclog.py`. The format is **ECS** (Elastic Common Schema):
one JSON object per line, so any ECS-aware SIEM (OpenSearch, Elastic, Wazuh,
Grafana) ingests it with no custom parsing. `seclog` is stdlib-only and
app-agnostic — point any future service at it with a different
`service.name` and its logs land in the same schema and dashboards.

## Where logs go

- **stdout** (one JSON line per event) — the 12-factor default; a container
  platform or a collector picks it up.
- **`logs/security.jsonl`** — a rotating file (size + backups configurable),
  which the local SIEM demo (`siem/`) tails with Vector.

Both are toggled in `config.py` (`log_to_stdout`, `log_to_file`, `log_dir`,
`log_file_max_mb`, `log_file_backups`).

## Event taxonomy (`event.action`)

| Action | Category | When | Notable fields |
|--------|----------|------|----------------|
| `app.started` | process | service boots | service.version, limit |
| `request.received` | web | each HTTP request begins | source.ip, http.request.method, url.path, user_agent.original |
| `request.completed` | web | each request ends | http.response.status_code, event.duration_ms |
| `request.error` | web | unhandled 5xx | error.type |
| `validation.refused` | file | a `SecurityError` (bad magic bytes, executable, bomb, oversize, bad extension) | labels.reason, labels.declared_type |
| `upload.too_large` | file | request exceeds the 5 MB cap (413) | labels.limit |
| `rate_limit.exceeded` | network | per-IP rate limit tripped (429) | labels.limit |
| `decode.completed` | file | an image decoded successfully | file.size, file.hash.sha256, labels.image_width/height, labels.streams, labels.content_sha256 |
| `code.detected` | intrusion_detection | decoded text looks like code | labels.code_findings, labels.content_sha256 |

Every event also carries: `@timestamp` (UTC), `ecs.version`, `log.level`,
`event.kind/category/type/outcome`, `service.name/version/environment`,
`host.name`, and `trace.id` (the per-request correlation id).

## What is never logged

The decoded message is untrusted and may contain sensitive or malicious
content, so **it is never written to a log**. Instead we log
`labels.content_sha256` (a hash) and, when relevant, `labels.code_findings`
(the category of code detected). `seclog` also scrubs a denylist of field
names (`password`, `token`, `authorization`, `cookie`, `secret`, `text`,
`content`, …) as a backstop, and never records file contents or request
bodies.

## Source IP privacy (GDPR)

IP addresses are personal data. `log_ip_mode` controls how they are stored:

| Mode | Example output | Use when |
|------|----------------|----------|
| `truncate` (default) | `203.0.113.0` (host bits zeroed) | you want geo/coarse grouping without storing the individual |
| `hash` | `sha256:95bbd140f1be0825` (salted) | you only need to recognise repeat offenders |
| `full` | `203.0.113.45` | you need blocking / precise geolocation and accept the retention duties |

Set it via `config.py` or the `DMB_LOG_IP_MODE` / `DMB_LOG_IP_SALT` env vars.
Pair this with an index retention policy in your SIEM.

## Using it from another app

```python
import seclog
log = seclog.get_logger({'log_service_name': 'my-other-app',
                         'log_service_version': '0.1.0'})
seclog.new_correlation()                      # once per request/task
log.event('validation.refused', category='file', outcome='failure',
          level='warning', source_ip=ip, reason='bad signature')
```

Same schema, same dashboards, same detection rules.

## Two projects, one SIEM

DrawMeBinary is delivered as two separate projects that both ship into the
**same** SIEM. They share this ECS schema and event taxonomy; only
`service.name` differs, so one index holds both and you can slice by project:

| Project | `service.name` | Status |
|---------|----------------|--------|
| Web app (this repo, `webapp/app.py`) | `drawmebinary-web` | shipping |
| Mobile app (camera decoder) | `drawmebinary-mobile` (suggested) | future, separate project |

Filter one project in Discover/Dev Tools with
`service.name: "drawmebinary-web"`.

When the mobile project is built, keep it consistent by emitting the **same**
ECS fields and actions rather than inventing new ones:

- Use the existing taxonomy: `app.started` for lifecycle, `decode.completed`
  per scan, `validation.refused` for rejected input, `code.detected` when
  decoded text looks like code.
- Reuse the field set in this doc (`file.size`, `file.hash.sha256`,
  `labels.image_width/height`, `labels.streams`, `labels.content_length`,
  `labels.content_sha256`, `event.duration_ms`, `trace.id`).
- Never log the decoded message, the photo, or a file path — only the
  `content_sha256` hash and metadata, exactly as the web app does.
- A camera scan has no client IP, so omit `source.ip` (or set a device /
  install id under a `labels.*` field if you need to group by device,
  treating it as personal data under the same retention policy).
- Mobile is offline-first: buffer events locally and flush to the collector
  when connectivity returns; the rotating-file + tail model here is the
  on-device analogue.

Because the schema is identical, the SIEM dashboards and detection monitors
in `siem/README.md` work for the mobile project unchanged.
