# CLI help — catalog (OpenMetadata)

Publish active ODCS contracts to a data-governance catalog, check sync status, or
remove Redibis-managed OpenMetadata entities.

Requires `pip install -e ".[catalog]"` and (for live OM writes) `OM_BOT_JWT`.

Contracts already exist? Enrich then push: [enrich.md](enrich.md) (LLM) → this
page. Air-gap cookbook: `enterprise/docs/install/enrich-and-catalog.md`.

## Env

```bash
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml
export OM_BOT_JWT='…'   # real JWT — placeholders like … are rejected
```

`catalog.backend: openmetadata` in YAML means you can omit `--backend openmetadata`.

## Which command?

| You have… | Command |
|-----------|---------|
| Active store row (`redibis show TABLE` works) | `redibis catalog push TABLE` |
| Every active table | `redibis catalog push --all` |
| One ODCS YAML/JSON on disk | `redibis catalog push-file FILE` |
| A folder of contract files (scan export) | `redibis catalog push-batch DIR [-r]` |

Table name for `push-file` / `push-batch` comes from `physicalName` (or
`database_name` + `table_name`) inside each file — no `schema.table` CLI arg.

## Push / status / open (active store)

```bash
redibis catalog backends
redibis catalog push telecom.customers --dry-run --json
redibis catalog push telecom.customers
redibis catalog push --all
redibis catalog push --tables 'telecom.*' --dry-run
redibis catalog status telecom.customers
redibis catalog open telecom.customers
```

`--tables GLOB` filters which tables a push (or enforce) applies to.
Audited overrides require `--enforce --reason …` (and `--confirm` to write).

Config key for publish path selection is
`catalog.openmetadata.entity_mode` (`enrich_existing` | `create_if_missing` |
`mixed`). Legacy `catalog.openmetadata.mode` is accepted for one release.

## Push one file

```bash
redibis catalog push-file ./telecom.customers.yaml --backend openmetadata --dry-run --json
redibis catalog push-file ./telecom.customers.yaml --backend openmetadata
```

## Push a scan / output folder (`push-batch`)

`push-file` is **one file**. For every `*.yaml` / `*.yml` / `*.json` in a directory:

```bash
redibis catalog push-batch ./contracts/ --backend openmetadata --dry-run --json
redibis catalog push-batch ./contracts/ --backend openmetadata

# recursive (typical scan / reports tree)
redibis catalog push-batch ./exports/ -r --backend openmetadata

# YAML only; stop on first error
redibis catalog push-batch ./exports/ -r --glob '*.yaml' --fail-fast \
  --backend openmetadata
```

Local scan store (same `--output-dir` you used on `scan`). The CLI/library
default is `./reports`; `scripts/batch_scan.sh` historically defaults to
`./scan_output` unless you pass `-o`. Target `active/` only — `no -r` needed,
it's flat, one file per table:

```bash
redibis catalog push-batch ./reports/_dev_storage/active-contracts/active \
  --backend openmetadata
# or, if you used the batch script default:
redibis catalog push-batch ./scan_output/_dev_storage/active-contracts/active \
  --backend openmetadata
```

**Do not** batch the whole `scan_output/<run_id>/` tree, and **do not** include
`active-contracts/audit/` — `audit/{table}/{uuid}.yaml` holds immutable
historical snapshots of the **same** table (re-pushing those is wasteful and can
blow past hundreds of files). Point `push-batch` at `active/` specifically.

If your scan used a nested `--output-dir` (e.g. `./scan_output/<workflow>/...`),
find the real path first: `find ./scan_output -type d -path '*active-contracts/active'`.

With MinIO/S3, prefer `redibis catalog push --all` (active bucket) over
`push-batch`.

### Quality test results (`catalog.push.quality`)

Default `true`. On each `catalog push`, redibis:

1. Upserts OM test suites / cases from the active contract's quality rules.
2. Attaches the latest results from `monitor/{table}/…/quality_results.json` when present.

Run monitoring first:

```bash
redibis quality-monitor run telecom.customers --sample ./data.csv --use-s3
redibis catalog push telecom.customers
```

Disable: `catalog.push.quality: false` in YAML. See [monitor.md](monitor.md).

## Feedback / ledger / suppressions

```bash
# Persist steward vetoes from OM change events
redibis catalog feedback sync

# Projection ledger
redibis catalog ledger show telecom.customers
redibis catalog ledger show --tables 'telecom.*'

# Suppressions (singular command)
redibis catalog suppression list telecom.customers
redibis catalog suppression add telecom.customers --kind tag --value PII.EMAIL
redibis catalog suppression remove telecom.customers --kind tag --value PII.EMAIL
```

## Delete selected tables

Dry-run by default. Pass `--yes` to execute.

```bash
# Preview
redibis catalog delete golden.tutorial_customers --config "$REDIBIS_CONFIG"

# Hard-delete one or more schema.table names
redibis catalog delete golden.tutorial_customers telecom.customers \
  --config "$REDIBIS_CONFIG" --yes

# Raw OpenMetadata FQNs
redibis catalog delete \
  --fqn redibis.golden.default.tutorial_customers \
  --config "$REDIBIS_CONFIG" --yes

# Soft-delete instead of hardDelete=true
redibis catalog delete golden.tutorial_customers --soft --yes
```

`schema.table` maps to `{service}.{database}.{default_schema}.{table}`
(e.g. `golden.tutorial_customers` → `redibis.golden.default.tutorial_customers`).

## Wipe the Redibis database service

Deletes the OM database service recursively (databases, schemas, tables under it).
Dry-run by default; `--yes` required to execute.

```bash
# Preview plan (lists tables under the service)
redibis catalog wipe --config "$REDIBIS_CONFIG"

# Wipe service tree
redibis catalog wipe --config "$REDIBIS_CONFIG" --yes

# Also remove glossary + Redibis / RedibisPolicy classifications
redibis catalog wipe --config "$REDIBIS_CONFIG" --yes \
  --with-glossary --with-classifications

# Non-default service name
redibis catalog wipe --service redibis --yes
```

## Safety

| Flag | Behavior |
|------|----------|
| *(default)* | Dry-run plan only (JSON); no OM deletes |
| `--yes` | Execute irreversible deletes |
| `--soft` | OM soft-delete (`hardDelete=false`) |
| `--json` | Always print machine-readable result |

Wipe / delete only remove **OpenMetadata** entities. Local/S3 active contracts and
run artifacts are untouched — clear those separately (`rm` / store tools).

## See also

- [enrich.md](enrich.md) — LLM-enrich active contracts before push
- Tutorial: [../tutorials/CATALOG_OPENMETADATA_TUTORIAL.md](../tutorials/CATALOG_OPENMETADATA_TUTORIAL.md)
- Golden CSV path: [../tutorials/PII_CSV_ENRICH_OPENMETADATA_CLI.md](../tutorials/PII_CSV_ENRICH_OPENMETADATA_CLI.md)
- Air-gap cookbook: `enterprise/docs/install/enrich-and-catalog.md`
- `redibis catalog delete --help` / `redibis catalog wipe --help`

## Push from a scan folder (`push-scan`)

Discovers `schema.table` from each `*/session.json` under a scan output dir, intersects
with **active** contracts in the store, then pushes with a durable run manifest.

```bash
# After batch_scan.sh / scan with automerge into the same --output-dir
redibis catalog push-scan ./scan_output --config "$REDIBIS_CONFIG"

# Subset / filters
redibis catalog push-scan ./scan_output --database telecom
redibis catalog push-scan ./scan_output --include 'telecom.*' --exclude '*.sample'
redibis catalog push-scan ./scan_output --tables-file ./tables.txt
redibis catalog push-scan ./scan_output --skip-in-sync

# Resume after failures (retry failed/pending; re-push if active version changed)
redibis catalog push-scan ./scan_output --resume <run_id>
redibis catalog push-scan ./scan_output --resume ./scan_output/catalog_push_runs/<run_id>/manifest.json
redibis catalog push-scan ./scan_output --resume <run_id> --no-retry-failed

# Machine-readable
redibis catalog push-scan ./scan_output --dry-run --json
```

### Manifest layout

```text
./scan_output/catalog_push_runs/<run_id>/
  manifest.json      # full run + per-table status
  succeeded.json
  failed.json
```

Per-table statuses: `pending`, `running`, `succeeded`, `failed`, `excluded`, `skipped`.
Exit code is `1` if any table remains `failed`.

### Filter order

1. Tables from scan sessions  
2. `--database` prefix  
3. `--include` / `--tables-file`  
4. `--exclude`  
5. Resume / `--skip-in-sync`

## OpenMetadata Data Contract strategy

| OM version | How the Data Contract is attached |
|------------|-----------------------------------|
| **≥ 1.12** | Current ODCS import: `PUT /v1/dataContracts/odcs` |
| **&lt; 1.12** (e.g. 1.11.13) | Native `CreateDataContract` mapper (`entityStatus`, not ODCS `status`) |

Table + tags + glossary still push on both. Disable only the contract attach with:

```yaml
catalog:
  push:
    contract: false
```
