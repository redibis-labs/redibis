# Web API — operator runbook

Guide for operators running the redibis FastAPI dashboard (`redibis.webapp.backend`). Covers health probes, in-process recovery, capacity tuning, and common failure modes.

Design rationale: [`WEB_ADMIN.md`](WEB_ADMIN.md). Implementation notes: [`WEB_ADMIN.md`](WEB_ADMIN.md).

---

## Quick checks

```bash
# Liveness — process is up (does NOT touch storage/DB; unauthenticated)
curl -s http://localhost:8000/health
# → {"status":"ok"}

# Readiness — dependencies usable (requires a session when auth is enabled)
# Sign in first: docs/DASHBOARD_AUTH.md#calling-the-api-curl
curl -s -b "$RB_COOKIES" http://localhost:8000/ready

# Force reconnect of cached storage handles (no process restart)
curl -s -b "$RB_COOKIES" -X POST http://localhost:8000/admin/reset \
  -H "X-CSRF-Token: $CSRF"
```

Replace host/port and credentials for your environment. See
[`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md) for login, roles, and TLS.

---

## Health vs readiness

| Endpoint | Purpose | Checks dependencies? | Typical use |
|---|---|---|---|
| `GET /health` | **Liveness** | No | Kubernetes liveness, “is the process alive?” |
| `GET /ready` | **Readiness** | Yes | Load balancer, systemd watchdog, auto-restart. **Requires a session** when auth is on. |

### `/health` response (always 200 when the process serves)

```json
{"status": "ok"}
```

Liveness only — no version, config, or store fields.

### `/ready` response

Returns **200** when all checks pass, **503** when any check fails:

```json
{
  "ready": true,
  "checks": {
    "storage": true,
    "config": true,
    "memory": true
  }
}
```

| Check | Passes when |
|---|---|
| `storage` | Storage backend `ping()` succeeds (S3/MinIO head-bucket or local root exists) |
| `config` | Global settings load within timeout (named-list registry) |
| `memory` | Memory disabled in config **or** the configured memory store is reachable |

Failed checks are `false`; the HTTP status is still 503 so supervisors can restart or drain the instance.

---

## In-process reset (no restart)

`POST /admin/reset` clears cached storage/service handles. The next API call rebuilds connections lazily.

**Authentication:** admin session cookie from `POST /login` (CSRF required on
mutating requests). HTTP Basic is no longer used. Default `admin`/`admin` is
not shipped — cookie-jar login is in [`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl).

```bash
curl -s -b "$RB_COOKIES" -X POST http://localhost:8000/admin/reset \
  -H "X-CSRF-Token: $CSRF"
# → {"reset": true}
```

Use when:

- Storage endpoint was temporarily unreachable and handles are stale
- You rotated S3/MinIO credentials and want a fresh client without restarting uvicorn
- `/ready` reports `storage: false` but the backend is actually back

**Does not:** flush in-memory scan sessions, cancel running jobs, or clear disk session folders. Evicted sessions can still be reloaded from `SCAN_OUTPUT_DIR`.

Change the default bootstrap password via `/api/users` after first deploy.

---

## Request tracing

Every HTTP request gets an `x-request-id` header (client may send one; otherwise the server generates a 12-char id).

On unhandled **500** errors the JSON body includes the same id:

```json
{
  "error": "internal_error",
  "correlation_id": "a1b2c3d4e5f6"
}
```

Correlate with structured logs via the `run_id` field in JSON log lines (`redibis.obs` context). See [`OBSERVABILITY.md`](OBSERVABILITY.md).

---

## Fetching logs

Where to read output depends on **how** the webapp was started. The API binds to **port 8000** by default (`REDIBIS_PORT` / `python -m redibis.webapp.backend`). If you see `Address already in use`, a copy is already running — tail its log file or attach to that terminal instead of starting a second process.

### Server logs (uvicorn / FastAPI)

| How started | Where logs go |
|---|---|
| Foreground `python -m redibis.webapp.backend` | The terminal where you launched it |
| `docker/scripts/deploy-changes.sh` (host mode) | `/tmp/uvicorn-redibis.log` → `tail -f /tmp/uvicorn-redibis.log` |
| Docker Compose (`run-local.sh`) | `docker logs -f <container>` or `docker exec <container> tail -f /tmp/uvicorn.log` |
| `deploy-changes.sh` (container mode) | `docker exec <container> tail -f /var/log/uvicorn.log` |

```bash
# Find the running process
ps aux | grep "redibis.webapp.backend"
ss -tlnp | grep :8000

# Restart in foreground (stop old server first)
fuser -k 8000/tcp    # or: kill <pid>
python -m redibis.webapp.backend
```

Structured JSON logs and level toggles: [`OBSERVABILITY.md`](OBSERVABILITY.md) (`REDIBIS_LOG_LEVEL`, `REDIBIS_LOG_FORMAT`, YAML `observability` block).

### Scan session logs (web UI)

| What | How |
|---|---|
| **Live during a scan** | Scan page log panel, or SSE `GET /api/sessions/{session_id}/stream` |
| **In-memory session** | `GET /api/sessions/{session_id}/debug` → `logs` (last 100 lines) |
| **After flush to disk** | `./scan_output/<session_id>/session.json` → `logs` |
| **UI** | Settings → **Debug** → select session → **open persisted** |

```bash
SESSION_ID=<uuid>

# Debug payload (includes logs + run summaries)
curl -s -b "$RB_COOKIES" "http://127.0.0.1:8000/api/sessions/${SESSION_ID}/debug" | jq '.logs'

# Persisted session file (after flush)
curl -s -b "$RB_COOKIES" "http://127.0.0.1:8000/api/scan_output/${SESSION_ID}" | jq '.logs'
# or on disk:
jq '.logs' "./scan_output/${SESSION_ID}/session.json"
```

Per-run step logs live under `runs[].logs` in the same JSON. After flush, each run also has artifacts under `scan_output/<session_id>/runs/<run_id>/` (`run_manifest.json`, `config_snapshot.json`, …).

**Flush sessions to disk** (Settings → Debug, or API):

```bash
curl -s -b "$RB_COOKIES" -X POST "http://127.0.0.1:8000/api/sessions/${SESSION_ID}/flush" \
  -H "X-CSRF-Token: $CSRF"
curl -s -b "$RB_COOKIES" -X POST "http://127.0.0.1:8000/api/sessions/flush_all" \
  -H "X-CSRF-Token: $CSRF"
```

Default session root: `SCAN_OUTPUT_DIR` (default `./scan_output`). Required for multi-worker uvicorn — see [Session store](#session-store-and-multi-worker-uvicorn) below.

### LLM call logs

Recent LiteLLM invocations are kept in an **in-memory ring buffer** (lost on process restart):

```bash
curl -s -b "$RB_COOKIES" 'http://127.0.0.1:8000/api/llm/calls?limit=50' | jq
```

UI: Settings → **Debug** → **LLM Debug** panel (↺ refresh loads on demand; not polled continuously).

### CLI / per-scan run logs

When running scans from the CLI with `observability.persist_run_log: true` (default), each run writes a log file under the output directory. See [`OBSERVABILITY.md`](OBSERVABILITY.md) for paths, decision-channel grep (`DECISION` lines), and OTel export.

### Agent / board runs

Live SSE: `GET /api/agents/runs/{run_id}/stream`. Audit DAG: `GET /api/agents/runs/{run_id}/trace`.

---


### Storage (unchanged)

| Variable | Default | Effect |
|---|---|---|
| `USE_LOCAL_STORAGE` | `false` | `true` → local FS under `LOCAL_STORAGE_ROOT` |
| `LOCAL_STORAGE_ROOT` | `./_local_storage` | Local backend root |
| `S3_ENDPOINT_URL`, `S3_*` | — | S3/MinIO connection (see `storage_backend.py`) |
| `S3_RUNS_BUCKET` | `pii-reports` | Run artifacts bucket |
| `S3_CONTRACTS_BUCKET` | `active-contracts` | Contracts bucket |
| `SCAN_OUTPUT_DIR` | `./scan_output` | Web session folders (required for multi-worker) |
| `CONFIGS_DIR` | `./configs` | Local named-list configs when using local storage |
| `REDIBIS_CONFIG` | — | Root YAML spine (`report.output_dir` default `./reports`; `evidence.restricted_spool_dir` default `./reports/_restricted_evidence`) |

Storage connects **lazily** on first use. Importing the app module does not require storage to be up.

### Reliability / capacity

| Variable | Default | Effect |
|---|---|---|
| `REDIBIS_SCAN_WORKERS` | `2` | Thread-pool workers for heavy scans (profile/quality/PII/discovery) |
| `REDIBIS_SCAN_QUEUE` | `10` | Max queued + in-flight scan jobs before **429** |
| `REDIBIS_MAX_SESSIONS` | `200` | In-memory session cache size |
| `REDIBIS_SESSION_TTL` | `21600` (6 h) | Evict idle sessions from memory after N seconds |
| `REDIBIS_STORE_TIMEOUT_SEC` | `30` | Config load timeout inside `/ready` |
| `REDIBIS_CORS_ORIGINS` | `*` | Comma-separated origins; `*` disables credentials |

### Process / deploy

| Variable | Default | Effect |
|---|---|---|
| `REDIBIS_PORT` | `8000` | Uvicorn port (systemd unit) |
| `REDIBIS_WORKERS` | `2` | Uvicorn worker processes |

---

## Scan jobs and load shedding

Heavy work (GE profiling, Presidio/GLiNER, unified scan, background discovery) runs on a **bounded thread pool**, not the FastAPI request threadpool.

- Failed background jobs set session status to `failed` and persist to disk in a `finally` block (sessions should not stay `running` forever).
- When the queue is saturated, new scan POSTs return **429** with a queue message.

Tune `REDIBIS_SCAN_WORKERS` to match CPU cores; start conservative (2) on shared hosts.

---

## Session store and multi-worker uvicorn

In-memory sessions are **bounded** (max count + TTL). Evicted sessions remain on disk under `SCAN_OUTPUT_DIR/<session_id>/`.

`GET /api/sessions/{id}` and the session loader **rehydrate** from disk when a session is not in memory — required for `--workers N > 1`.

**Rule:** run multiple uvicorn workers only when `SCAN_OUTPUT_DIR` is on **shared storage** visible to all workers (NFS, PVC, etc.), or accept that a session created on worker A may need a disk rehydrate on worker B.

### Quality review — Jupyter code and monitor package export

The interactive review page and the **Data quality** results page both offer:

- **Copy Jupyter Code** — one complete, self-contained Spark-first `.py` program
  for the rules currently kept: imports, an inline `RULES = [...]` literal,
  `build_rule_set()`, `load_data()`, and `validate(df)`. It is Jupyter-ready as
  copied — paste it in, edit the rules, and call `validate(existing_dataframe)`.
  No Redibis-generated JSON or YAML is read at runtime
- **Download monitor package** — a zip with that same program plus the canonical
  `quality/rule_set.json`, legacy `quality/rules.yaml`, the rules-only
  `ge_paste.py` compatibility fragment, JSON Schemas, `requirements.txt`, and
  `spark-submit` Airflow DAGs (see [`cli/monitor.md`](cli/monitor.md) for the
  full layout)

Rows removed on the review page are excluded from both (`dropped_indices`). Both
actions share one resolver and one renderer with the CLI, so the copied code and
the packaged program never drift apart.

```bash
# full Jupyter-ready program
curl -s -b "$RB_COOKIES" -X POST \
  "http://127.0.0.1:8000/api/sessions/${SESSION_ID}/quality/full-code" \
  -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"engine":"spark"}' -o table_quality.py

# deployment package
curl -s -b "$RB_COOKIES" -X POST \
  "http://127.0.0.1:8000/api/sessions/${SESSION_ID}/quality/export-package" \
  -H "X-CSRF-Token: $CSRF" -o monitor-package.zip
```

`full-code` returns `text/x-python` with `X-Redibis-Rule-Source` and
`X-Redibis-Rule-Count` headers. Pass `{"rules": [...]}` for already-curated rules
or `{"code": "..."}` for pasted Python; text is parsed with AST + literals only.

### Bringing edited Python back as proposed rules

The Quality page's paste panel accepts a rules-only fragment **or** a full
generated program, and **📄 load .py file** loads an edited program from disk into
the same textarea. Parsing is one-way and never executes the file: it yields
proposed rules that you evaluate, approve, and merge through the existing
approval flow — the only path that writes a contract.

CLI equivalents: `redibis quality-monitor export <table> -o ./packages` and
`redibis quality-monitor export <table> --python-file ./edited_quality.py -o ./packages`.
See [`cli/monitor.md`](cli/monitor.md).

---

## systemd deployment

Bare-metal unit: [`enterprise/deploy/airgap/baremetal/redibis-web.service`](DEPLOY.md)

```ini
Restart=on-failure
RestartSec=3
StartLimitIntervalSec=0
ExecStart=... uvicorn redibis.webapp.backend:app --workers ${REDIBIS_WORKERS} ...
```

Recommended additions:

1. **Readiness watchdog** — timer or sidecar that curls `/ready` **with a session** (auth is on by default; unauthenticated `/ready` is 401). Restart the unit if unready for > N seconds.
2. **Liveness** — hit `/health` only (do not use `/ready` for liveness or a storage blip will kill the process).
3. **Shared `SCAN_OUTPUT_DIR`** when `REDIBIS_WORKERS` > 1.

Example watchdog script (run from cron or a companion timer):

```bash
#!/bin/bash
READY_URL="${REDIBIS_READY_URL:-http://127.0.0.1:8000/ready}"
FAIL_SECS="${REDIBIS_UNREADY_MAX_SECS:-120}"
# ... track consecutive failures, systemctl restart redibis-web when over threshold
```

---

## Graceful degradation

| Dependency | When unavailable |
|---|---|
| Memory / pgvector | Scans and classification continue; similarity hints skipped. `/ready` `memory: false` if memory is enabled but unreachable. |
| Observability (OTel) | Startup is non-fatal; the app still serves if `init_otel` fails. |
| Storage (transient) | `/ready` → 503; after recovery use `POST /admin/reset` or wait for supervisor restart. |

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| API up but all contract routes 503/500 | Dead storage handle | `POST /admin/reset` or restart; check MinIO/S3 |
| `/ready` 503, `storage: false` | Bucket unreachable or creds wrong | Fix endpoint; reset handles |
| `/ready` 503, `config: false` | Config store slow or missing | Check `CONFIGS_DIR` or object-store config keys |
| `/ready` 503, `memory: false` | pgvector down while `memory.enabled` | Fix Postgres DSN or disable memory in YAML |
| UI frozen under heavy scans | Thread pool saturated | Raise `REDIBIS_SCAN_WORKERS` or shed load (429) |
| Session “not found” after idle | Evicted from memory | Reload via `/api/sessions/load` or open session URL again (rehydrates from disk) |
| Scan stuck `running` | Pre-reliability bug or manual kill | Check `session.json` status; re-run scan; upgrade if `finally` persist missing |
| `Address already in use` on port 8000 | Previous uvicorn still running | `ps` / `fuser -k 8000/tcp`; tail existing log — see [Fetching logs](#fetching-logs) |
| Flood of `GET /api/llm/calls` | Stale UI auto-refresh loop | Hard-refresh browser; upgrade static `app.js` (LLM debug loads once per visit) |
| Scan stuck, UI shows "Scanning…" forever | Suspended server process (Ctrl+Z / SIGSTOP) | See [Frozen / suspended server process](#frozen--suspended-server-process) below |
| Debug → flush freezes the whole UI | Synchronous flush + heavy debug render | Upgrade to async flush; see [Debug tab freeze](#debug-tab-freeze) below |

### Frozen / suspended server process

If the scan UI stays on "Scanning…" and the server terminal is unresponsive, the uvicorn process may be **suspended** (SIGSTOP). This commonly happens when you press **Ctrl+Z** in the terminal where the server runs — it stops the process instead of killing it.

**Diagnose:**

```bash
# STAT column shows "T" (stopped) or "Tl" (stopped, multi-threaded)
ps -o pid,stat,cmd -p $(pgrep -f "redibis.webapp.backend")

# Health check times out (server not responding)
curl -s -m 3 http://127.0.0.1:8000/health
```

**Resume (without restart):**

```bash
# Send SIGCONT to wake the process
kill -CONT $(pgrep -f "redibis.webapp.backend")

# Verify it responds again
curl -s http://127.0.0.1:8000/health
```

**Clean restart (recommended):**

```bash
pkill -f "redibis.webapp.backend"
sleep 1
nohup python -m redibis.webapp.backend > /tmp/uvicorn-redibis.log 2>&1 &
tail -f /tmp/uvicorn-redibis.log
```

Then hard-refresh the browser (Ctrl+Shift+R) and re-run the scan.

**Prevention:** never press **Ctrl+Z** in the server terminal. Use **Ctrl+C** to stop, or run the server in the background with `nohup` and tail the log in a separate terminal.

**Check session status after recovery:**

```bash
# List all sessions and their status
curl -s -b "$RB_COOKIES" http://127.0.0.1:8000/api/sessions | \
  jq '.[] | {id: .session_id, status, table: .table_name}'

# Cancel a stuck session
curl -s -b "$RB_COOKIES" -X DELETE http://127.0.0.1:8000/api/sessions/<SESSION_ID> \
  -H "X-CSRF-Token: $CSRF"
```

| Session `status` stuck on | Meaning |
|---|---|
| `profiling` | GE OnboardingDataAssistant running (can be slow on large files) |
| `running_quality` | Quality expectations executing |
| `running_pii` | Presidio / GLiNER detection (first NER model load is slow) |
| `running` / `initialized` | Job never started — check log for `429` (queue full) |
| `scan_complete` | Normal terminal state — scan finished successfully |
| `error` / `failed` | Scan failed; check `logs` in session debug payload |

### Debug tab freeze

The Settings → Debug page can freeze the browser when:

1. **Flush blocks the event loop** — `persist_to_disk()` runs synchronously inside an async route. Large sessions with many runs block the entire FastAPI process until done. Fix: flush routes use `asyncio.to_thread()`.
2. **Heavy HTML rendering** — Debug view stringifies the full session JSON + every run's `config_snapshot` (regex sets, quality rules) on every render. Fix: config snapshots load lazily on expand; raw JSON is truncated above ~150 KB; `regex_set` / `quality_rule_set` show summaries.
3. **LLM calls auto-refresh loop** — When the LLM call list is empty, `loadLlmCalls()` was called on every render, each call triggers `render()`, creating an infinite loop that floods `GET /api/llm/calls`. Fix: `llmCallsFetched` flag ensures a single fetch per visit; refresh is manual only.

All three are fixed in the current codebase. If you see the old behavior, redeploy static assets and hard-refresh the browser.

### Starting and stopping the server

```bash
# Start in background (recommended for development)
nohup python -m redibis.webapp.backend > /tmp/uvicorn-redibis.log 2>&1 &
tail -f /tmp/uvicorn-redibis.log      # watch logs in a separate terminal

# Start in foreground (logs go to stdout — do NOT press Ctrl+Z)
python -m redibis.webapp.backend

# Stop cleanly
pkill -f "redibis.webapp.backend"
# or by port:
fuser -k 8000/tcp

# Restart via deploy script (host mode — kills old, starts new, waits for health)
./docker/scripts/deploy-changes.sh --host
```

---

## Development: code reload and NER verification

Operators and developers running the dashboard locally need to know when a **server restart** is required vs when **auto-reload** is enough, and how to confirm the **NER (GLiNER)** engine is actually running — not just Presidio regex.

Full NER download, packaging, and upload: [`ner_models.md`](ner_models.md). PII scan quick start: [README — PII scan with GLiNER](../README.md#pii-scan-with-gliner).

### Picking up code changes

| Change type | What to do |
|---|---|
| Python under `redibis/` | Auto-reload when started via `python -m redibis.webapp.backend` or `uvicorn … --reload` (default for the module entry point). Watch the terminal for `WatchFiles detected changes … Reloading`. |
| Static JS/CSS (`redibis/webapp/static/`) | Hard-refresh the browser (`Ctrl+Shift+R` / `Ctrl+F5`). Restart the server if the browser still serves a cached bundle. |
| Jinja HTML templates | `auto_reload=True` on the template env — save and refresh; no restart usually needed. |
| New pip extra (`pip install -e ".[ner-runtime]"`) | **Full restart** — reload does not re-import installed packages. |
| NER model weights swapped on disk | **Full restart** — GLiNER loads weights once per process. |
| Env vars (`REDIBIS_NER_MODEL`, storage paths) | **Full restart** — read at process start (session overrides are separate). |

```bash
# Auto-reload (development default)
python -m redibis.webapp.backend
# equivalent:
uvicorn redibis.webapp.backend:app --reload --host 0.0.0.0 --port 8000

# Manual restart
pkill -f "redibis.webapp.backend"
python -m redibis.webapp.backend

# Liveness only — does NOT prove NER is loaded
curl -s http://localhost:8000/health
```

**Note:** `GET /health` and `GET /ready` confirm the API and dependencies are up; they do **not** report whether a NER model is configured or loaded.

### Running NER from the dashboard

redibis uses two PII engines:

| Engine | Library | What you see in logs |
|---|---|---|
| **Regex** | Presidio recognizers | `Fetching all recognizers for language en` |
| **NER** | GLiNER (local weights) | `↳ Running NER model (gliner:…)` |

Presidio log lines alone do **not** mean NER is active.

**Prerequisites** (once per machine):

```bash
pip install -e ".[ner-runtime]" -c requirements/constraints.txt
./scripts/download_ner_model.sh   # → ./models/gliner-multi-v2.1/
export REDIBIS_MODELS_DIR=./models
export REDIBIS_NER_MODEL=./models/gliner-multi-v2.1
```

Restart the server with those env vars set, then in the UI:

1. **Settings → NER model** — select or activate the model for the session (or upload via the models API).
2. Set **Engines** to **Both (regex + NER)** — not regex-only.
3. Run a **PII** or **Both** scan.

**CLI smoke test** (isolated from the UI):

```bash
python -m redibis.cli.main scan \
  tests/data/realistic_merchant_seller_registry.csv merchant.sellers \
  --mode pii \
  --pii-engines gliner \
  --ner-model ./models/gliner-multi-v2.1 \
  --output-dir ./reports
```

**List discovered models:**

```bash
python -m redibis.cli.main pii ner list
python -m redibis.cli.main models list
curl -s -b "$RB_COOKIES" http://localhost:8000/api/pii/ner/models
curl -s -b "$RB_COOKIES" "http://localhost:8000/api/models"
```

### Verifying NER is working

#### Signs NER **is** running

| Signal | Where to look |
|---|---|
| Server log | `↳ Running NER model (gliner:./models/gliner-multi-v2.1) on 'column_name'…` |
| Scan UI | **GLiNER** column shows a numeric score (e.g. `0.85`), not `—` |
| Detection payload | `gliner_score`, `gliner_label`, `ner_engine` populated (e.g. `"gliner:./models/gliner-multi-v2.1"`) |
| PII report HTML | Engine line: `ner: matched` or `ner: no_match` (not `not_run` / `backend_unavailable`) |
| Name-like columns | PERSON/name columns scored when regex is weak — typical NER value-add |

#### Signs NER is **not** running

| Signal | Meaning |
|---|---|
| Log: `No NER model_path configured; NER engine disabled (regex-only)` | No model path in env/YAML/session |
| Log: `NER backend unavailable: …` | Missing `gliner` package, bad path, or corrupt weights |
| Engine status `backend_unavailable` | NER requested but backend failed to load |
| All `gliner_score` null / UI shows `—` | NER did not run or found no entities |
| Engines set to `regex` only | NER intentionally disabled |

#### NER skipped on a column (model loaded, column skipped)

If regex scores a column ≥ the configured regex floor (~0.80 by default), NER may be **skipped** unless “always run NER” is enabled. Engine status may be `skipped_high_regex` — the model is loaded; that column did not need a neural pass.

#### Grep persisted scan logs

With `observability.decision_log: true` (default), NER pass lines are emitted from `pii.detector._run_ner`. In a per-scan log file or the session SSE stream:

```bash
grep -E "Running NER model|NER backend unavailable|No NER model_path" \
  ./scan_output/<session_id>/runs/<run_id>/*.log 2>/dev/null

# Session debug API (in-memory or after flush)
curl -s -b "$RB_COOKIES" "http://127.0.0.1:8000/api/sessions/<SESSION_ID>/debug" | \
  jq '.logs[]' | grep -i ner
```

See [`OBSERVABILITY.md` — Decision channel](OBSERVABILITY.md#decision-channel) for structured `DECISION` lines and log paths.

### NER troubleshooting quick reference

| Symptom | Action |
|---|---|
| Only Presidio lines in server terminal | Set engines to `both` or `gliner`; activate model in Settings |
| `backend_unavailable` in scan results | Check `pip show gliner`, model path exists, restart after `pip install -e ".[ner-runtime]"` |
| GLiNER column always `—` | Confirm `REDIBIS_NER_MODEL` before server start; re-run PII scan |
| First PII scan very slow | Normal — GLiNER loads weights on first use (`running_pii` in session status) |
| Model list empty | Run `./scripts/download_ner_model.sh`; set `REDIBIS_MODELS_DIR` |

---

## Related docs

- [`OBSERVABILITY.md`](OBSERVABILITY.md) — logging, OTel, correlation fields, NER decision lines
- [`ner_models.md`](ner_models.md) — download, upload, Docker mounts, NER troubleshooting
- [`DEPLOY.md`](DEPLOY.md) — users, roles, share links
- [`DEPLOY.md`](DEPLOY.md) — deploy overview
