# Local SIEM demo

Ship DrawMeBinary's security logs into a real SIEM you can run on your laptop:

```
Vector  ──tail──▶  OpenSearch  ──▶  OpenSearch Dashboards
(collector)        (index/store)     (search · dashboards · alerts)
```

Everything here is Apache-2.0 and self-hosted, matching the "self-hosted
open-source" direction. It's a **local demo** — the OpenSearch security plugin
is disabled so there's no login. Don't expose these ports publicly as-is.

## Prerequisites

- Docker + Docker Compose
- ~2 GB free RAM (OpenSearch is a JVM service)

## Run it

```bash
# 1. produce some logs (from the project root)
python webapp/app.py            # use the UI a few times, upload good/bad files
#   or run the CLI:  python drawmebinary/main.py test/test_stop.png -b
#   logs are written to   logs/security.jsonl

# 2. start the stack
docker compose -f siem/docker-compose.yml up -d

# 3. open Dashboards
open http://localhost:5601
#   Stack Management ▸ Index Patterns ▸ create  drawmebinary-logs-*
#   (time field: @timestamp)   then go to Discover
```

To stop and wipe: `docker compose -f siem/docker-compose.yml down -v`.

## What you'll see

Every event is ECS JSON. Useful fields in Discover / queries:

| Field | Meaning |
|-------|---------|
| `event.action` | the taxonomy verb (`validation.refused`, `code.detected`, …) |
| `event.outcome` | `success` / `failure` |
| `source.ip` | client IP (truncated by default — see SECURITY_LOGGING.md) |
| `trace.id` | correlation id tying one request's events together |
| `http.response.status_code`, `event.duration_ms` | per-request access info |
| `labels.reason` | why a request was refused |
| `labels.code_findings` | what made decoded text look like code |
| `labels.content_sha256` | hash of decoded text (never the text itself) |

## Starter detection queries (Discover / Dev Tools)

Paste these into **Dev Tools** (`http://localhost:5601/app/dev_tools`):

Refusals grouped by source IP (scanning / fuzzing):
```json
GET drawmebinary-logs-*/_search
{ "size": 0, "query": { "term": { "event.action": "validation.refused" } },
  "aggs": { "by_ip": { "terms": { "field": "source.ip", "size": 10 } } } }
```

Anyone uploading code-like payloads:
```json
GET drawmebinary-logs-*/_search
{ "query": { "term": { "event.action": "code.detected" } },
  "sort": [ { "@timestamp": "desc" } ] }
```

5xx errors in the last 15 minutes:
```json
GET drawmebinary-logs-*/_search
{ "query": { "bool": { "filter": [
  { "term": { "event.action": "request.error" } },
  { "range": { "@timestamp": { "gte": "now-15m" } } } ] } } }
```

## Turning these into alerts

OpenSearch Dashboards ▸ **Alerting** ▸ Monitors lets you schedule any of the
queries above and notify (email, Slack, webhook) on a trigger. Suggested
monitors to start with:

| Monitor | Trigger | Why |
|---------|---------|-----|
| Refusal spike per IP | > 20 `validation.refused` from one `source.ip` in 5 min | scanning for an upload bypass |
| Code payloads | any `code.detected` | someone is probing what the decoder does with code |
| Rate-limit abuse | > 50 `rate_limit.exceeded` from one IP in 10 min | brute force / DoS attempt |
| Error surge | > 10 `request.error` in 5 min | a bug or an exploitation attempt |
| Oversize floods | > 20 `upload.too_large` in 5 min | resource-exhaustion attempt |

OpenSearch also ships a **Security Analytics** plugin (Sigma-rule based) if you
later want pre-built detections and a correlation engine.

## Production notes (when you actually expose the app)

- Enable the OpenSearch security plugin (auth + TLS); never run with
  `DISABLE_SECURITY_PLUGIN`.
- Ship logs **off the app host** (as Vector does here to a separate service)
  so an attacker who lands on the box can't erase them. Consider an append-only
  or write-once destination for integrity.
- Keep clocks in sync (NTP) so timestamps correlate across services.
- Set an index lifecycle / retention policy that matches your data-retention
  obligations (source IPs are personal data under GDPR).
- Run Vector (or Filebeat / Fluent Bit) as a sidecar/agent per host; point all
  future apps' `seclog` output at the same pipeline and they land in the same
  schema automatically.
