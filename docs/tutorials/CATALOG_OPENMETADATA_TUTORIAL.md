# OpenMetadata Catalog Tutorial — push, check, delete & wipe

End-to-end guide for publishing **active ODCS contracts** from redibis to **OpenMetadata**
using the `redibis catalog` CLI, plus cleanup via `catalog delete` / `catalog wipe`.

---

## What gets pushed

For each `schema.table` with an **active contract** in your contract store, redibis:

1. Creates/updates the OM hierarchy: **service → database → schema → table**
2. Maps columns (types, descriptions, PII tags)
3. Optionally upserts **glossary terms** under glossary `Redibis`
4. Attaches the full **ODCS v3 contract** as a table **Data Contract**
5. When `catalog.push.quality: true` (default), upserts **Data Quality test suites / cases**
   and publishes the latest **monitor run results** from `monitor/{table}/…/quality_results.json`

Table FQN pattern:

```text
{service}.{database}.{schema}.{table_name}
```

Example: `telecom.customers` → `redibis.telecom.default.customers`

---

## Prerequisites

```bash
cd /path/to/redibis
pip install -e ".[catalog,dev]" -c requirements/constraints.txt
```

| Requirement | Notes |
|-------------|-------|
| **OpenMetadata ≥ 1.8** | Running and reachable. **1.12+** uses ODCS Data Contract import; **&lt; 1.12** (e.g. 1.11.13) uses the native mapper automatically |
| **Bot JWT** | `OM_BOT_JWT` env var (see below) |
| **Active contract** | Table must exist in contract store (`redibis list` or dashboard merge) |
| **`httpx`** | Installed via `redibis[catalog]` |

### 1. Start OpenMetadata (local)

With the redibis Docker stack:

```bash
./deployment_scripts/redibis.sh up --build --with-openmetadata
```

Or OM alone from the sandbox:

```bash
cd open_metadata && docker compose up -d
```

Wait until the API answers:

```bash
curl -sf http://localhost:8585/api/v1/system/version | head -c 200
```

### 2. Create a bot token in OpenMetadata UI

1. Open **http://localhost:8585** and sign in (basic auth; self-signup enabled in the sandbox compose).
2. Go to **Settings → Bots** (or **Users → Bots** depending on OM version).
3. Create or open a bot (e.g. `redibis-bot`) with permissions to create/update tables, tags, glossaries, and data contracts.
4. Copy the **JWT** and export it:

```bash
export OM_BOT_JWT='eyJ...'   # paste token here — never commit this
```

### 3. Choose catalog backend (config file)

Pick **one** profile and point redibis at it. Table name (`schema.table`) is read
from each contract file automatically — no `--table` flag needed.

**OpenMetadata** — copy or use `config/examples/catalog-openmetadata.yaml`:

```yaml
catalog:
  backend: openmetadata
  openmetadata:
    host: http://localhost:8585
    jwt_env: OM_BOT_JWT
```

```bash
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml
export OM_BOT_JWT='...'
```

**DataHub** — `config/examples/catalog-datahub.yaml`:

```yaml
catalog:
  backend: datahub
  datahub:
    host: http://localhost:8080
    token_env: DATAHUB_GMS_TOKEN
```

```bash
export REDIBIS_CONFIG=config/examples/catalog-datahub.yaml
export DATAHUB_GMS_TOKEN='...'
```

**Apache Atlas** — `config/examples/catalog-atlas.yaml`:

```yaml
catalog:
  backend: atlas
  atlas:
    host: http://localhost:21000
    username_env: ATLAS_USERNAME
    password_env: ATLAS_PASSWORD
```

```bash
export REDIBIS_CONFIG=config/examples/catalog-atlas.yaml
export ATLAS_USERNAME=admin
export ATLAS_PASSWORD=admin
```

Verify active backend:

```bash
redibis catalog backends
# Config: config/examples/catalog-datahub.yaml
# Active backend: datahub
```

You can still pass `--config path.yaml` per command instead of `REDIBIS_CONFIG`, or
`--backend atlas` for a one-shot override without editing the file.

### 4. Point at your contract store (store-based push only)

**Local dev** (default — filesystem under `./reports/_dev_storage`):

```bash
export REDIBIS_OUTPUT_DIR=./reports
```

**MinIO / S3** (production):

```bash
export S3_ENDPOINT_URL=http://localhost:9000
export S3_ACCESS_KEY=minioadmin
export S3_SECRET_KEY=minioadmin
export S3_CONTRACTS_BUCKET=pii-contracts
# CLI flag:
# redibis --use-s3 catalog push telecom.customers
```

---

## CLI command reference

```bash
redibis catalog backends              # list publishers (openmetadata, atlas, datahub)

# From contract store (active contracts in S3 / local store)
redibis catalog push TABLE
redibis catalog push --all            # continues on table errors by default
redibis catalog push --all --fail-fast

# From a scan-output folder (session.json → active contracts) with resume
redibis catalog push-scan ./scan_output
redibis catalog push-scan ./scan_output --database telecom --exclude '*.sample'
redibis catalog push-scan ./scan_output --resume <run_id>
redibis catalog push-scan ./scan_output --dry-run --json

# From contract files — table from physicalName inside each file
redibis catalog push-file CONTRACT.yaml
redibis catalog push-batch ./contracts/
redibis catalog push-batch ./contracts/ -r

redibis catalog push TABLE --dry-run
redibis catalog status TABLE          # last push vs current contract version
redibis catalog open TABLE            # print OpenMetadata UI URL

# Cleanup (dry-run unless --yes)
redibis catalog delete TABLE …        # remove selected OM tables
redibis catalog wipe                  # remove OM database service tree
```

**OpenMetadata Data Contract strategy:** OM **1.12+** uses `PUT /v1/dataContracts/odcs`.
OM **&lt; 1.12** (including 1.11.13) uses a native `CreateDataContract` mapper (`entityStatus`).
Failures are recorded per table; the batch continues unless `--fail-fast`.

Common flags:

| Flag | Purpose |
|------|---------|
| `--config path.yaml` | Catalog backend + host credentials (or `REDIBIS_CONFIG` env) |
| `--backend openmetadata\|datahub\|atlas` | One-shot override of `catalog.backend` |
| `--use-s3` | Read contracts from S3/MinIO instead of local storage |
| `--json` | Machine-readable output |
| `--yes` | Required on `delete` / `wipe` to execute (default is dry-run) |
| `--soft` | Soft-delete in OM instead of `hardDelete=true` |

---

## Push from contract files (no store required)

Use these when you have ODCS YAML/JSON on disk and **do not** need the contract
in the redibis contract store first.

### Single file

```bash
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml
export OM_BOT_JWT='...'

# schema.table resolved from physicalName / database_name+table_name in the file
redibis catalog push-file ./telecom.customers.yaml

# Same file, push to DataHub instead (swap config + token)
export REDIBIS_CONFIG=config/examples/catalog-datahub.yaml
export DATAHUB_GMS_TOKEN='...'
redibis catalog push-file ./telecom.customers.yaml

# Preview payloads
redibis catalog push-file ./telecom.customers.yaml --dry-run --json
```

**Table resolution** (automatic from contract — no CLI flag):

1. Top-level or `schema[].physicalName` (e.g. `telecom.customers`)
2. `database_name` + `table_name` fields
3. Filename stem when it looks like `db.table` (e.g. `telecom.customers.yaml`)

### Batch (directory)

```bash
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml

# Every *.yaml / *.yml / *.json in the folder
redibis catalog push-batch ./contracts/

# Recursive + custom glob
redibis catalog push-batch ./exports/ -r --glob '*.yaml'

# Stop on first failure
redibis catalog push-batch ./contracts/ --fail-fast
```

Example layout:

```text
contracts/
  telecom.customers.yaml
  telecom.cdr_event.yaml
  retail.orders.json
```

```bash
redibis catalog push-batch ./contracts/
```

Output:

```text
pushed telecom.customers → openmetadata (redibis.telecom.default.customers)
  open: http://localhost:8585/table/redibis.telecom.default.customers
pushed telecom.cdr_event → openmetadata (redibis.telecom.default.cdr_event)
  open: http://localhost:8585/table/redibis.telecom.default.cdr_event
```

If a file fails, batch continues unless `--fail-fast` is set; exit code is `1` when
any file fails.

> **Telemetry:** `push-file` / `push-batch` only update `catalog status` in the
> contract store when that table already has an active contract there. OM always
> receives the push regardless.

---

## Push from a scan folder (`push-scan`)

Use this after a batch scan that wrote session folders **and** merged active contracts
into the same `--output-dir` / store.

```bash
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml
export OM_BOT_JWT='...'
OUT=./scan_output

# Discover tables from */session.json, push matching active contracts
redibis catalog push-scan "$OUT" --config "$REDIBIS_CONFIG" --output-dir "$OUT"

# Filters
redibis catalog push-scan "$OUT" --database telecom --exclude '*.sample'
redibis catalog push-scan "$OUT" --include 'telecom.customers' 'telecom.orders'
redibis catalog push-scan "$OUT" --tables-file ./want.txt --skip-in-sync

# Resume
redibis catalog push-scan "$OUT" --resume <run_id>
redibis catalog push-scan "$OUT" --resume "$OUT/catalog_push_runs/<run_id>/manifest.json"

# Dry-run JSON report
redibis catalog push-scan "$OUT" --dry-run --json
```

### What gets selected

1. Read each `SCAN_DIR/*/session.json` → `table_name` (`schema.table`)
2. Deduplicate
3. Keep only tables that have an **active** contract in the store
4. Apply `--database` / `--include` / `--tables-file` / `--exclude`
5. Optionally exclude already-in-sync tables (`--skip-in-sync`)

Tables without an active contract are marked `excluded` in the manifest (not a hard failure).

### Manifest & resume

Each run writes:

```text
SCAN_DIR/catalog_push_runs/<run_id>/
  manifest.json
  succeeded.json
  failed.json
```

| Status | Meaning |
|--------|---------|
| `succeeded` | Push completed for this table |
| `failed` | Error recorded; batch continues (unless `--fail-fast`) |
| `excluded` | Filtered out or no active contract / already in sync |
| `pending` / `running` | Not finished — picked up by `--resume` |

`--resume` retries `failed` + `pending` by default. Successful rows are skipped unless the
active contract **version** changed since the recorded success. Use `--no-retry-failed` to
leave previous failures alone.

### OpenMetadata Data Contract attach

| OM server | Strategy |
|-----------|----------|
| **1.12+** | `PUT /v1/dataContracts/odcs` (current ODCS import) |
| **&lt; 1.12** (e.g. **1.11.13**) | Native `CreateDataContract` payload (`entityStatus`) |

Version is read once per batch from `/api/v1/system/version` and cached. A contract attach
failure flags that table and continues; it does not abort the whole scan batch.

To push table/tags/glossary only:

```yaml
catalog:
  push:
    tags: true
    glossary: true
    contract: false
    quality: false
```

---

## Data quality test results

After rules are merged into the active contract, run continuous monitoring:

```bash
redibis quality-monitor run "$TABLE" --sample /path/to/sample.csv --config "$CONFIG" --use-s3
# or batch:
redibis quality-monitor batch --all-contracts --config "$CONFIG" --use-s3 --json
```

Each monitor run writes `quality_results.json` (plus the canonical
`redibis.io/quality/v1alpha1` `quality_run.json`) to the runs bucket and (unless
`--no-publish`) dispatches results to every configured result sink.
`catalog.push.quality: true` is shorthand for `quality.publish.sinks: [openmetadata]`;
to publish to more than one destination (once other sinks exist), set
`quality.publish.sinks` explicitly — see [`../cli/monitor.md`](../cli/monitor.md#canonical-schema-and-result-sinks).

`redibis catalog push` also replays the **latest** monitor results (selected by
recorded completion time, not run-id) when `catalog.push.quality` is enabled
(default `true`).

In OpenMetadata:

1. Open the table → **Profiler & Data Quality** (or **Quality** tab, depending on OM version).
2. Inspect test suite `redibis_{table}` and per-rule history.
3. Configure **Observability → Alerts** (Slack, email, generic webhook) on test failures.

Disable quality publishing only:

```yaml
catalog:
  push:
    quality: false
```

CLI reference: [../cli/monitor.md](../cli/monitor.md).

---

## Push from contract store

Set a table you already have a contract for:

```bash
export TABLE=telecom.customers   # schema.table — must match physicalName
export CONFIG=redibis.yaml       # optional
```

### Step 1 — Confirm an active contract exists

```bash
redibis list --use-s3                    # or without --use-s3 for local store
redibis show "$TABLE" --use-s3
```

If empty, run a scan + merge first (dashboard **Approved → merge**, or `redibis merge`).

### Step 2 — Dry-run (preview OM payloads)

```bash
redibis catalog push "$TABLE" --dry-run --json --config "$CONFIG" --use-s3
```

Inspect `steps[]` in the JSON: `databaseService`, `database`, `databaseSchema`, `table`, `glossaryTerms`, `dataContract`.

### Step 3 — Push to OpenMetadata

```bash
redibis catalog push "$TABLE" --config "$CONFIG" --use-s3
```

Expected output:

```text
pushed telecom.customers → openmetadata (redibis.telecom.default.customers)
  open: http://localhost:8585/table/redibis.telecom.default.customers
```

Push every contracted table:

```bash
redibis catalog push --all --config "$CONFIG" --use-s3
```

### Step 4 — Check push status (in sync?)

```bash
redibis catalog status "$TABLE" --config "$CONFIG" --use-s3
```

Example:

```text
telecom.customers  backend=openmetadata  contract_version=1.2.0
  last push: 2026-06-14T12:00:00Z  fqn=redibis.telecom.default.customers
  in_sync: True
  open: http://localhost:8585/table/redibis.telecom.default.customers
```

- `in_sync: True` — catalog copy matches the active contract version.
- `in_sync: False` — contract changed since last push; run `catalog push` again.

JSON:

```bash
redibis catalog status "$TABLE" --json --use-s3
```

### Step 5 — Open the contract in OpenMetadata

**Option A — CLI prints the URL**

```bash
redibis catalog open "$TABLE" --config "$CONFIG"
# http://localhost:8585/table/redibis.telecom.default.customers
```

**Option B — Navigate in the UI**

1. Open **http://localhost:8585**
2. **Explore → Tables** (or search the table name)
3. Path: **Services → redibis → {database} → default → {table}**
4. On the table page, open the **Data Contract** tab to see the ODCS document redibis pushed.

**What to verify in OM**

| Area | What redibis wrote |
|------|-------------------|
| **Columns** | Names, types, descriptions |
| **Tags** | `PII.Sensitive` / `PII.NonSensitive`, `Redibis.{ENTITY}` |
| **Glossary** | Terms under glossary **Redibis** (if enabled) |
| **Data Contract** | Full ODCS YAML/JSON from your active contract |

---

## HTTP API (dashboard / automation)

Same operations from the running FastAPI app. Auth is on by default — sign in
first ([`DASHBOARD_AUTH.md`](../DASHBOARD_AUTH.md#calling-the-api-curl)).
Port **8080** is the Docker local dashboard; use **8000** for `redibis web`.

```bash
# Push
curl -s -b "$RB_COOKIES" -X POST "http://localhost:8000/api/catalog/push/telecom.customers?dry_run=false" \
  -H "X-CSRF-Token: $CSRF"

# Status
curl -s -b "$RB_COOKIES" "http://localhost:8000/api/catalog/status/telecom.customers"
```

Requires `OM_BOT_JWT` in the server environment and contracts reachable from the app’s store config.

---

## Delete tables or wipe the Redibis OM service

Destructive OpenMetadata cleanup for entities created by `catalog push`.
**Without `--yes` these commands only print a dry-run plan.**

### Delete selected tables

```bash
# Preview
redibis catalog delete golden.tutorial_customers --config "$CONFIG"

# Execute hard-delete
redibis catalog delete golden.tutorial_customers --config "$CONFIG" --yes

# Multiple tables and/or raw FQNs
redibis catalog delete telecom.customers retail.orders \
  --fqn redibis.golden.default.tutorial_customers \
  --config "$CONFIG" --yes
```

`schema.table` maps to `{service}.{database}.{default_schema}.{table}`
(e.g. `golden.tutorial_customers` → `redibis.golden.default.tutorial_customers`).

### Wipe the whole Redibis database service

Deletes the OM database service recursively (databases + schemas + tables under it):

```bash
# Preview what would be removed
redibis catalog wipe --config "$CONFIG"

# Wipe service tree
redibis catalog wipe --config "$CONFIG" --yes

# Also remove Redibis glossary + Redibis/RedibisPolicy classifications
redibis catalog wipe --config "$CONFIG" --yes \
  --with-glossary --with-classifications
```

Use `--service NAME` to target a non-default service name.
Use `--soft` for OM soft-delete instead of `hardDelete=true`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `OpenMetadata JWT missing` | `export OM_BOT_JWT=...` |
| `No active contract for 'x.y'` | Merge a contract first; check `--use-s3` vs local store |
| `httpx is required` | `pip install -e ".[catalog]"` |
| `401` / `403` from OM | Bot lacks permissions; regenerate JWT |
| Connection refused to `:8585` | Start OM (`--with-openmetadata`) or fix `catalog.openmetadata.host` |
| Unrecognized field `"status"` on Data Contract | OM &lt; 1.12 — redibis uses the native mapper automatically; upgrade to 1.12+ for ODCS import, or set `catalog.push.contract: false` |
| Table in OM but no Data Contract | Check `catalog.push.contract: true`; on 1.11.x verify native attach succeeded in logs / `failed.json` |
| `push-scan` finds no tables | Ensure `SCAN_DIR/*/session.json` has `table_name`, and those tables exist in `redibis list --output-dir …` |
| Want to retry only failures | `redibis catalog push-scan DIR --resume <run_id>` |
| Wrong FQN in UI | Check `physicalName` on contract matches `schema.table` |
| `catalog delete/wipe: dry-run only` | Re-run with `--yes` to execute |
| Delete reports `missing` | Table FQN wrong or already wiped; check `--fqn` / `service_name` |
| Wipe left glossary/tags | Pass `--with-glossary --with-classifications` |

---

## Quick copy-paste cheat sheet

```bash
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml
export OM_BOT_JWT='...'
OUT=./scan_output

# From scan folder (preferred after batch scan)
redibis catalog push-scan "$OUT" --config "$REDIBIS_CONFIG" --output-dir "$OUT"
redibis catalog push-scan "$OUT" --resume <run_id>

# From file (backend + table from config + contract)
redibis catalog push-file ./telecom.customers.yaml
redibis catalog push-batch ./contracts/
redibis catalog push-batch ./scan_output/_dev_storage/active-contracts/active

# From contract store
export TABLE=telecom.customers
redibis catalog push "$TABLE" --use-s3
redibis catalog status "$TABLE" --use-s3
redibis catalog open "$TABLE"

# Cleanup (preview, then execute)
redibis catalog delete "$TABLE" --config "$REDIBIS_CONFIG"
redibis catalog delete "$TABLE" --config "$REDIBIS_CONFIG" --yes
redibis catalog wipe --config "$REDIBIS_CONFIG" --yes \
  --with-glossary --with-classifications
```

---

## Related docs

- [../cli/catalog.md](../cli/catalog.md) — CLI cheat sheet (push / push-file / push-batch / delete / wipe)
- [../cli/enrich.md](../cli/enrich.md) — LLM-enrich before push
- Air-gap cookbook: `enterprise/docs/install/enrich-and-catalog.md`
- [../../docker/README.md](../../docker/README.md) — start OM with `--with-openmetadata`
- [../REDIBIS_COMPLETE_USER_GUIDE.md](../REDIBIS_COMPLETE_USER_GUIDE.md) — broader deployment patterns
- `redibis catalog --help` — live CLI help