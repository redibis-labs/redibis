# Observability runbook

Turn on structured logging, per-scan log persistence, and optional OpenTelemetry on a deployed redibis host.

## Quick toggles (CLI)

```bash
# DEBUG + JSON logs on stderr
redibis --debug --log-format json scan data.csv demo.table --output-dir ./reports

# Plain logs (log aggregation friendly)
redibis --log-format plain scan data.csv demo.table
```

Global flags apply to every subcommand.

## Environment variables (override YAML)

| Variable | Values | Effect |
|---|---|---|
| `REDIBIS_LOG_LEVEL` | `DEBUG`, `INFO`, … | Root log level |
| `REDIBIS_LOG_FORMAT` | `rich`, `json`, `plain` | Formatter (default: rich on TTY, else json) |
| `REDIBIS_LOG_MODULES` | `name=LEVEL,...` | Per-logger overrides, e.g. `great_expectations=WARNING,redibis.pii=DEBUG` |

## YAML (`observability` block)

```yaml
observability:
  log_level: INFO
  log_format: json
  persist_run_log: true
  decision_log: true
  module_levels:
    great_expectations: WARNING
  otel:
    enabled: false
    exporter: file          # file (default) | otlp | prometheus
    otlp_endpoint: ""
    metrics: true
    service_name: redibis
```

Env vars win over YAML.

## Per-scan log file

When `persist_run_log: true` (default), each scan writes:

```
<output_dir>/<run_id>/<table>.<run_id>.log
```

A best-effort **local filesystem** mirror may also appear under:

```
<run_dir>/_meta/logs/<schema>/<table>/<run_id>.log
```

This mirror is **not** written to S3/MinIO — on object-storage deployments the durable artifact is the run-dir file on the compute volume (or your log shipper). Route through your storage backend if you need bucket persistence.

The same capture buffer feeds the web SSE streams (`/api/sessions/{sid}/stream`, `/api/agents/runs/{run_id}/stream`).

**Web dashboard operators:** server log locations, session flush paths, LLM debug API, and curl examples — [`WEB_ADMIN.md`](WEB_ADMIN.md#fetching-logs).

## Decision channel

When `observability.decision_log: true` (default), verdicts emit to logger `redibis.decision` at **INFO** with a structured `decision` payload. Pattern metadata (`regex`, `pattern_name`, `match_count`, …) is preserved; raw cell keys (`value`, `sample`, `example`, …) are stripped. Free-text `rule`/`verdict` fields are redacted when they look like cell payloads.

Wire points:

- `pii.detector._run_presidio` / `pii.detector._run_ner` — per-pattern and per-NER-pass hits (see [`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md))
- `pii.equations.decide_pii`
- `quality.gatekeeper.run_tests`
- `masking.plan.suggest_rule`
- `profiling.triage`

Grep persisted per-scan logs for `DECISION` lines to explain “why was column X flagged?”.

### NER (GLiNER) log signals

Do not confuse **Presidio regex** lines (`Fetching all recognizers for language en`) with **NER** — they are separate engines.

| Log line | Meaning |
|---|---|
| `↳ Running NER model (gliner:…)` | NER backend loaded and scoring a column |
| `No NER model_path configured; NER engine disabled (regex-only)` | No local weights path — regex-only PII |
| `NER backend unavailable: …` | Missing `gliner` extra, bad path, or load error |

In scan results, check `gliner_score` / `ner_engine` on detections, or the PII report engine line `ner: matched|no_match|backend_unavailable|not_run`. Full operator runbook (reload, dashboard setup, verification): [`WEB_ADMIN.md` — Development: code reload and NER verification](WEB_ADMIN.md#development-code-reload-and-ner-verification).

**Console vs file:** `capture_run_log` temporarily lowers the root logger to DEBUG so GE/Presidio DEBUG lands in the per-scan file, but the console handler keeps your configured level (e.g. INFO) so the server terminal is not flooded.

**Remote WARNING:** If `REDIBIS_LOG_LEVEL=WARNING`, decision lines are hidden from the console but still captured in the per-scan file (capture forces DEBUG on the root logger during scans).

Set `decision_log: false` in YAML to silence the channel entirely.

## OpenTelemetry (optional)

Install the extra on connected hosts:

```bash
pip install -e ".[otel]"
```

Enable in YAML:

```yaml
observability:
  otel:
    enabled: true
    exporter: file    # air-gap safe default
```

- **file** — spans under `<run_dir>/_meta/telemetry/spans.jsonl` (no network)
- **otlp** — gRPC export; degrades to file if endpoint missing/unreachable
- **prometheus** — requires `opentelemetry-exporter-prometheus`; exposes metrics when configured

The in-process `TelemetryCollector.export()` and agent DAG trace remain unchanged — OTel is additive behind the same API.

## Audit DAG

Agent/board runs: `GET /api/agents/runs/{run_id}/trace` (React Flow audit). Spans come from `run_context(run_id)` regardless of OTel.

## Open questions

- Default consumer for Deep Scan synthesis bundles (enrich LLM path vs agent node vs external) — see `docs/DEEP_SCAN.md`.
- Per-scan log retention on busy servers — align with `_meta` retention policy when defined.
