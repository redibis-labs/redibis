# redibis Services Layer — Architecture & API Reference

Documentation for `redibis.services`. This is the reference an LLM or
developer needs to build a REST API, CLI, or GUI on top of redibis.

---

## Architecture

```
           ┌──────────────────────┐
           │  Adapters (thin)     │
           │  ┌────────────────┐  │
           │  │ REST (FastAPI)  │  │
           │  │ CLI (Click)     │  │
           │  │ GUI (React/Vue) │  │
           │  └───────┬────────┘  │
           └──────────┼───────────┘
                      │ calls
      ┌───────────────┼───────────────┐
      │               │               │
  ┌───▼───┐     ┌─────▼────┐    ┌────▼────────┐
  │ Scan  │     │  Browse  │    │  Contract   │
  │Service│     │  Service │    │  Service    │
  └───┬───┘     └────┬─────┘    └────┬────────┘
      │              │               │
      └──────────────┼───────────────┘
                     │ uses
          ┌──────────▼──────────┐
          │  Core modules       │
          │  ┌────────────────┐ │
          │  │ ContractStore  │ │
          │  │ StorageBackend │ │
          │  │ ContractMerger │ │
          │  │ QualityProfiler│ │
          │  │ QualityGate... │ │
          │  │ PIIDetector    │ │
          │  │ EquationEngine │ │
          │  │ ContractWriter │ │
          │  │ RunOutputWriter│ │
          │  └────────────────┘ │
          └─────────────────────┘
```

### The rule

Adapters (REST, CLI, GUI) contain ZERO business logic. They:
1. Parse user input (form data, CLI args, uploaded files)
2. Call one service method
3. Format the response (JSON, HTML, table, download link)

If you're writing an `if` statement that decides what to do with
data — it belongs in a service, not an adapter. If you're importing
`QualityProfiler` or `ContractStore` directly in a REST endpoint —
stop, call the service instead.

---

## Bootstrapping

Every service needs a `StorageBackend` and a `ContractStore`. Create
them once and share across all services:

```python
from redibis.store.storage_backend import LocalBackend, S3Backend, S3Config
from redibis.store.contract_store import ContractStore
from redibis.services import ScanService, BrowseService, ContractService

# ── Local development ──────────────────────────────
backend = LocalBackend("./dev_storage")
store   = ContractStore(backend, bucket="contracts")

# ── Production (S3/MinIO) ──────────────────────────
# backend = S3Backend(S3Config(
#     endpoint_url = "https://minio.example.com:9000",
#     access_key   = "...",
#     secret_key   = "...",
# ))
# store = ContractStore(backend, bucket="pii-contracts")

# ── Create services ────────────────────────────────
scan_svc     = ScanService(backend=backend, store=store)
browse_svc   = BrowseService(backend=backend, store=store)
contract_svc = ContractService(backend=backend, store=store)
```

---

## Service 1 — ScanService

**Purpose:** Takes a file or DataFrame, runs the full quality + PII
pipeline, saves everything to storage, returns links.

**When the UI calls it:** User uploads a CSV, clicks "Scan", sees a
progress screen, then gets a results page with links to reports.

### Methods

#### `scan_csv(csv_path, config) → ScanResult`

Reads a CSV from disk. Use for CLI and scheduled jobs.

```python
result = scan_svc.scan_csv(
    csv_path = "/data/telecom_customers.csv",
    config   = ScanConfig(table="telecom.customers"),
)
```

#### `scan_bytes(file_bytes, filename, config) → ScanResult`

Parses uploaded file bytes. Use for REST file upload.
Supports CSV, Parquet, and Excel.

```python
# In a REST endpoint:
result = scan_svc.scan_bytes(
    file_bytes = request.file.read(),
    filename   = "customers.csv",
    config     = ScanConfig(table="telecom.customers"),
)
```

#### `scan_dataframe(df, config) → ScanResult`

Core method. All others delegate here.

```python
result = scan_svc.scan_dataframe(
    df     = my_dataframe,
    config = ScanConfig(
        table            = "telecom.customers",
        equation_mode    = "balanced",        # strict | balanced | lenient
        run_pii          = True,              # False = quality-only scan
        run_quality      = True,
        generate_ge_docs = True,              # GE Data Docs HTML
        validate_contracts = True,            # lint ODCS before storing
        triage_threshold = 0.30,              # column triage cutoff
        output_dir       = Path("./output"),  # local artifact dir
    ),
)
```

### ScanConfig fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `table` | str | required | Table identifier, e.g. `"telecom.customers"` |
| `equation_mode` | str | `"balanced"` | PII equation: `strict`, `balanced`, `lenient` |
| `sampling_config` | SamplingConfig | None | Sampling strategy (if None, use full DataFrame) |
| `thresholds` | Thresholds | None | Per-engine confidence floors (if None, use defaults) |
| `triage_threshold` | float | 0.30 | Column triage score cutoff for PII scanning |
| `run_pii` | bool | True | Whether to run PII detection |
| `run_quality` | bool | True | Whether to run quality profiling |
| `generate_ge_docs` | bool | True | Whether to generate GE Data Docs HTML |
| `validate_contracts` | bool | True | Whether to lint contracts before storing |
| `output_dir` | Path | `./reports` | Local directory for artifacts (CLI `--output-dir` default) |

### ScanResult fields

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | str | Timestamp-based run identifier |
| `table` | str | The scanned table |
| `status` | str | `"success"` or `"failed"` |
| `error` | str | Error message if failed |
| `total_rows` | int | Row count of the scanned data |
| `total_columns` | int | Column count |
| `quality_expectations` | int | Number of quality rules evaluated |
| `quality_passed` | int | Rules that passed |
| `quality_failed` | int | Rules that failed |
| `pii_columns_scanned` | int | Columns sent to PII detector |
| `pii_columns_detected` | int | Columns where PII was detected |
| `arabic_aware_columns` | int | Columns with Arabic content |
| `quality_contract_version` | str | Contract version after quality upsert |
| `pii_contract_version` | str | Contract version after PII upsert |
| `artifacts` | dict | Map of artifact name → file path or S3 key |
| `started_at` | str | ISO timestamp |
| `completed_at` | str | ISO timestamp |
| `duration_seconds` | float | Total pipeline duration |

### Artifacts produced

The `artifacts` dict contains paths/keys to all generated files:

| Key | File | Description |
|-----|------|-------------|
| `interactive_review` | `interactive_review.html` | Grouped interactive rule curation page |
| `triage_report` | `triage_report.html` | Column triage scores table |
| `quality_report` | `quality-report-{run_id}.html` | Custom quality results report |
| `ge_report` | `ge_report/index.html` | Full GE Data Docs site (if generated) |
| `quality_contract` | `quality_contract.yaml` | Quality-only ODCS contract |
| `pii_contract` | `pii_contract.yaml` | PII-only ODCS contract |
| `pii_detections` | `pii_detections.json` | Per-column PII detection details (`llm_reasoning` omitted) |
| `evidence_bundle` | `evidence_bundle.json` | Local raw evidence (never uploaded) |
| `evidence_manifest` | `evidence_manifest.json` | Coverage, `run_status`, artifact hashes |
| `s3:*` | various | S3 keys for uploaded artifacts |

### Pipeline steps (what happens inside)

```
1. Parse input        → DataFrame
2. Sample             → DataFrame (optionally reduced)
3. Profile            → QualityProfiler.run_assistant()
4. Arabic detection   → QualityProfiler.profile_arabic_presence()
5. Column triage      → QualityProfiler.compute_column_profiles()
6. Interactive review → profiler.export_interactive_review()
7. Triage report      → profiler.export_triage_report()
8. Quality gate       → QualityGatekeeper.run_tests()
9. Quality report     → gatekeeper.export_quality_report()
10. GE Data Docs      → gatekeeper.copy_data_docs_to()
11. Quality contract  → gatekeeper.export_quality_contract()
12. PII detection     → PIIDetector.detect() (or legacy detect_pii)
13. Equation          → decide_pii() on each detection
14. PII contract      → PIIContractWriter.build()
15. Upload to S3      → RunOutputWriter.write_file() per artifact
16. Upsert contracts  → ContractStore.upsert() for quality and PII
17. Write manifest    → run_manifest.json
```

### Error handling

If any step fails, the service catches the exception, sets
`result.status = "failed"` and `result.error = str(e)`, writes
the partial manifest to S3, and returns. The caller (REST/CLI)
decides how to present the error.

---

## Service 1b — ContinuousQualityService

**Purpose:** Validate **approved active-contract** quality rules on a schedule or after
ETL — without re-profiling or calling `ContractStore.upsert()`.

**When the CLI/UI calls it:** `redibis quality-monitor run|batch`, Airflow DAGs, or
`POST /api/sessions/{sid}/quality/export-package` (package only).

```python
from redibis.services.continuous_quality import ContinuousQualityService, MonitorRunOptions

svc = ContinuousQualityService.from_config(config)
result = svc.run_table(
    "telecom.customers",
    options=MonitorRunOptions(sample_path="/data/sample.csv", publish_catalog=True),
)
# result.artifact_ref → monitor/{table}/{run_id}/quality_results.json
```

Library validate-only entry: `redibis.quality.contract_validate.validate_contract_quality()`.

Operator docs: [`cli/monitor.md`](cli/monitor.md).

---

## Service 2 — BrowseService

**Purpose:** Read-only access to contracts, history, scan runs,
and artifacts. No writes, no side effects.

**When the UI calls it:** User opens the dashboard, sees a list of
tables, clicks one to see the contract, clicks history to see changes,
clicks a run to see its artifacts.

### Methods

#### Contract browsing

```python
# List all tables with summaries
tables = browse_svc.list_tables()
# Returns: list[ContractSummary]
# Each has: table, name, version, status, total_columns,
#           pii_columns, quality_rules, last_updated, highest_sensitivity

# Get full contract
contract = browse_svc.get_contract("telecom.customers")
# Returns: dict (the full ODCS contract)

# Get as YAML string (for display in a code block)
yaml_str = browse_svc.get_contract_yaml("telecom.customers")

# Get lightweight summary
summary = browse_svc.get_contract_summary("telecom.customers")
# Returns: ContractSummary

# Export to another format
soda_yaml = browse_svc.export_contract("telecom.customers", "sodacl")
dbt_yaml  = browse_svc.export_contract("telecom.customers", "dbt-sources")
html      = browse_svc.export_contract("telecom.customers", "html")
```

#### History and versioning

```python
# Audit trail
history = browse_svc.get_history("telecom.customers", limit=50)
# Returns: list[TableHistoryEntry]
# Each has: run_uuid, run_id, workflow, timestamp, version,
#           audit_key, contributed_fields

# Get contract at a specific point in time
old_contract = browse_svc.get_contract_at_version(
    "telecom.customers",
    run_uuid="abc-123-def",
)

# Diff two versions
diff = browse_svc.diff_contracts(
    "telecom.customers",
    uuid_a="abc-123",  # older
    uuid_b="def-456",  # newer
)
# Returns: {
#     "added":   ["new_column"],
#     "removed": ["old_column"],
#     "changed": {
#         "phone": {
#             "classification": {"before": None, "after": "pii_personal"},
#             "pii": {"before": None, "after": {...}},
#         }
#     },
#     "version_a": "1.0.0",
#     "version_b": "1.1.0",
# }
```

#### Scan run browsing

```python
# List runs (all tables)
runs = browse_svc.list_runs(limit=20)
# Returns: list[RunSummary]

# List runs for a specific table
runs = browse_svc.list_runs(table="telecom.customers", limit=10)

# Filter by workflow
runs = browse_svc.list_runs(table="telecom.customers", workflow="quality")

# Get a specific run manifest
manifest = browse_svc.get_run("telecom.customers", "2026-05-13_10-00-00")
# Returns: dict (the ScanResult.to_dict() that was saved)

# Get a specific artifact from a run
html_content = browse_svc.get_run_artifact(
    "telecom.customers",
    "2026-05-13_10-00-00",
    "interactive_review.html",
)
```

### ContractSummary fields

| Field | Type | Description |
|-------|------|-------------|
| `table` | str | Table identifier |
| `name` | str | Contract name |
| `version` | str | Current version |
| `status` | str | `active`, `draft`, etc. |
| `total_columns` | int | Number of columns in the schema |
| `pii_columns` | int | Columns flagged as PII |
| `quality_rules` | int | Total quality rules (table + column level) |
| `last_updated` | str | Last modification timestamp |
| `highest_sensitivity` | str | `pii_sensitive`, `pii_personal`, `internal` |

### RunSummary fields

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | str | Run identifier |
| `table` | str | Scanned table |
| `workflow` | str | `quality`, `pii`, `business`, `scan` |
| `status` | str | `success` or `failed` |
| `timestamp` | str | When the run happened |
| `quality_passed` | int | Rules passed |
| `quality_failed` | int | Rules failed |
| `pii_detected` | int | PII columns found |
| `artifacts` | dict | Map of artifact name → path |

---

## Service 3 — ContractService

**Purpose:** Business users update existing contracts with definitions,
descriptions, SQL constraints, tags, and classification overrides.
This is Workflow C — no profiling, no PII detection, just enrichment.

**When the UI calls it:** A data steward opens a contract, edits a
column description, adds a business rule, clicks "Save".

### Methods

#### Column definitions

```python
from redibis.services import ColumnDefinition

contract_svc.update_column_definitions(
    table="telecom.customers",
    columns=[
        ColumnDefinition(
            column                = "phone",
            description           = "Customer primary contact number in E.164 format",
            business_name         = "Primary Phone",
            tags                  = ["contact", "regulated"],
            critical_data_element = True,
        ),
        ColumnDefinition(
            column         = "national_id",
            description    = "Egyptian national ID number (14 digits)",
            business_name  = "National ID",
            classification = "pii_sensitive",
            examples       = ["29001011400001"],
        ),
    ],
)
```

#### Quick single-field updates

```python
# Set one column's description
contract_svc.set_column_description(
    "telecom.customers", "phone",
    "Customer primary contact number in E.164 format",
)

# Set table-level description
contract_svc.set_table_description(
    "telecom.customers",
    description = "Customer master data for billing and support",
    purpose     = "360-degree customer view",
)

# Add tags (merge as set union, never overwrites existing tags)
contract_svc.add_tags("telecom.customers", ["regulated", "gdpr"])

# Override PII classification when automated detection is wrong
contract_svc.override_classification(
    "telecom.customers", "city", "internal",  # was flagged as PII, actually public
)
```

#### SQL business rules

```python
from redibis.services import SQLConstraint

contract_svc.add_sql_constraints(
    table="telecom.customers",
    constraints=[
        SQLConstraint(
            query       = "SELECT * FROM ${object} WHERE balance < 0 AND status = 'active'",
            description = "Active customers cannot have negative balance",
            severity    = "error",
            dimension   = "accuracy",
        ),
        SQLConstraint(
            query       = "SELECT * FROM ${object} WHERE phone IS NULL AND account_type = 'postpaid'",
            description = "Postpaid accounts must have a phone number",
            max_failures = 0,
        ),
    ],
)
```

#### Full update (all at once)

```python
from redibis.services import BusinessUpdate, ColumnDefinition, SQLConstraint

contract_svc.apply_business_update(BusinessUpdate(
    table             = "telecom.customers",
    table_description = "Customer master data",
    table_purpose     = "360-degree customer view for billing and support",
    domain            = "customer",
    owner             = "data-engineering",
    tags              = ["regulated", "gdpr"],
    columns           = [
        ColumnDefinition(column="phone", description="Primary contact (E.164)"),
        ColumnDefinition(column="email", description="Customer email address"),
    ],
    sql_constraints   = [
        SQLConstraint(
            query="SELECT * FROM ${object} WHERE balance < 0 AND status = 'active'",
            description="No negative balance for active customers",
        ),
    ],
))
```

#### Import from YAML file

```python
# Single table
contract_svc.apply_business_yaml(
    table     = "telecom.customers",
    yaml_path = "business_definitions/customers.yaml",
)

# Bulk: multiple tables from one file
results = contract_svc.import_business_file("business_glossary.yaml")
for r in results:
    print(f"  {r.table}: v{r.version_after}")
```

### YAML file format (single table)

```yaml
# business_definitions/customers.yaml
description: "Customer master data for billing and support"
purpose: "360-degree customer view"
domain: "customer"
owner: "data-engineering"
tags: ["regulated", "gdpr"]

columns:
  phone:
    description: "Customer primary contact number in E.164 format"
    business_name: "Primary Phone"
    tags: ["contact", "regulated"]
    critical_data_element: true
  national_id:
    description: "Egyptian national ID number (14 digits)"
    classification: "pii_sensitive"
    examples: ["29001011400001"]
  email:
    description: "Customer email address"
    business_name: "Email"
  city:
    description: "Customer city of residence"
    examples: ["Cairo", "Alexandria", "Giza"]

sql_constraints:
  - query: "SELECT * FROM ${object} WHERE balance < 0 AND status = 'active'"
    description: "Active customers cannot have negative balance"
  - query: "SELECT * FROM ${object} WHERE phone IS NULL AND account_type = 'postpaid'"
    description: "Postpaid accounts must have a phone number"
```

### YAML file format (bulk — multiple tables)

```yaml
# business_glossary.yaml
tables:
  telecom.customers:
    description: "Customer master data"
    columns:
      phone:
        description: "Primary contact (E.164)"
      email:
        description: "Customer email"
  telecom.orders:
    description: "Order transaction records"
    columns:
      order_id:
        description: "Unique order identifier"
      total:
        description: "Order total in EGP"
```

### ColumnDefinition fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `column` | str | required | Column name (must match the schema) |
| `description` | str | `""` | Human-readable description of what this column means |
| `business_name` | str | `""` | Business-friendly display name |
| `classification` | str | None | Override: `pii_sensitive`, `pii_personal`, `internal`, `public` |
| `tags` | list | `[]` | Tags (merged as set union with existing tags) |
| `is_pii_override` | bool | None | Override the automated PII detection (True = force PII, False = force not-PII) |
| `critical_data_element` | bool | False | Mark as a critical data element for governance |
| `examples` | list | `[]` | Example values for documentation |

### SQLConstraint fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | str | required | SQL query that returns violating rows. Use `${object}` for the table name. |
| `description` | str | `""` | Human-readable description of the business rule |
| `max_failures` | int | 0 | Maximum allowed violations (0 = zero tolerance) |
| `severity` | str | `"error"` | `error` or `warning` |
| `dimension` | str | `"accuracy"` | Data quality dimension: `accuracy`, `completeness`, `consistency`, `timeliness` |

---

## How the three services interact

They don't call each other. They share `ContractStore` and
`StorageBackend` as dependencies, but each service operates
independently. The contract store handles merge conflicts.

```
ScanService.scan_csv()
    → store.upsert(quality_contract, workflow="quality")
    → store.upsert(pii_contract,     workflow="pii")

ContractService.apply_business_yaml()
    → store.upsert(business_partial,  workflow="business")

BrowseService.get_contract()
    → store.get_active()  ← returns the MERGED result of all three
```

The `ContractStore.upsert()` merger combines contributions from all
three workflows. Each workflow owns its fields:

| Field | Quality | PII | Business |
|-------|---------|-----|----------|
| `quality[]` | ✓ writes | — | ✓ adds SQL rules |
| `pii` | — | ✓ writes | — |
| `classification` | — | ✓ writes | ✓ can override |
| `tags` | ✓ adds | ✓ adds | ✓ adds (set union) |
| `description` | — | — | ✓ writes |
| `businessName` | — | — | ✓ writes |
| `required` | ✓ writes | — | — |
| `unique` | ✓ writes | — | — |
| `criticalDataElement` | — | — | ✓ writes |

---

## UI mapping guide

For an LLM building the GUI, here's which service method to call for
each screen/action:

### Dashboard / Home screen

| UI element | Service call |
|------------|-------------|
| Table list with badges | `browse_svc.list_tables()` |
| Click table → contract view | `browse_svc.get_contract(table)` |
| Contract YAML viewer | `browse_svc.get_contract_yaml(table)` |
| Recent scans list | `browse_svc.list_runs(limit=10)` |

### Contract detail screen

| UI element | Service call |
|------------|-------------|
| Contract header (name, version, status) | `browse_svc.get_contract_summary(table)` |
| Schema properties table | `browse_svc.get_contract(table)` → `schema[0].properties` |
| Quality rules list | extract from `properties[].quality` |
| PII badges per column | extract from `properties[].pii` or `properties[].classification` |
| Export dropdown (Soda, dbt, SQL) | `browse_svc.export_contract(table, format)` |
| History tab | `browse_svc.get_history(table)` |
| Compare versions | `browse_svc.diff_contracts(table, uuid_a, uuid_b)` |

### Scan screen

| UI element | Service call |
|------------|-------------|
| Upload CSV + click "Scan" | `scan_svc.scan_bytes(file_bytes, filename, config)` |
| Progress display | Poll `result.status` (or use async in REST) |
| Results page | Read `result.artifacts` for links |
| Open interactive review | Serve `result.artifacts["interactive_review"]` |
| Open quality report | Serve `result.artifacts["quality_report"]` |
| Open GE Data Docs | Serve `result.artifacts["ge_report"]` |

### Edit contract screen

| UI element | Service call |
|------------|-------------|
| Edit column description | `contract_svc.set_column_description(table, col, desc)` |
| Edit table description | `contract_svc.set_table_description(table, desc, purpose)` |
| Add tags | `contract_svc.add_tags(table, tags)` |
| Override classification | `contract_svc.override_classification(table, col, class)` |
| Add SQL rule | `contract_svc.add_sql_constraints(table, [SQLConstraint(...)])` |
| Bulk update form | `contract_svc.apply_business_update(BusinessUpdate(...))` |
| Import YAML file | `contract_svc.apply_business_yaml(table, yaml_path)` |

### Run detail screen

| UI element | Service call |
|------------|-------------|
| Run metadata | `browse_svc.get_run(table, run_id)` |
| Artifact list | Read `manifest["artifacts"]` dict |
| View artifact | `browse_svc.get_run_artifact(table, run_id, name)` |

---

## REST endpoint mapping (for reference)

These routes sit behind dashboard auth (cookie + CSRF on writes).
See [`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl). This table
is a service-to-path map, not a curl tutorial.

The REST adapter would map these service calls to endpoints:

```
# ── Scan ───────────────────────────────────────────
POST   /api/scan                        → scan_svc.scan_bytes()
GET    /api/scan/{table}/{run_id}       → browse_svc.get_run()

# ── Browse contracts ───────────────────────────────
GET    /api/contracts                   → browse_svc.list_tables()
GET    /api/contracts/{table}           → browse_svc.get_contract()
GET    /api/contracts/{table}/yaml      → browse_svc.get_contract_yaml()
GET    /api/contracts/{table}/summary   → browse_svc.get_contract_summary()
GET    /api/contracts/{table}/history   → browse_svc.get_history()
GET    /api/contracts/{table}/export/{format} → browse_svc.export_contract()
GET    /api/contracts/{table}/diff      → browse_svc.diff_contracts()

# ── Browse runs ────────────────────────────────────
GET    /api/runs                        → browse_svc.list_runs()
GET    /api/runs/{table}                → browse_svc.list_runs(table)
GET    /api/runs/{table}/{run_id}       → browse_svc.get_run()
GET    /api/runs/{table}/{run_id}/{artifact} → browse_svc.get_run_artifact()

# ── Update contracts ───────────────────────────────
PUT    /api/contracts/{table}/columns   → contract_svc.update_column_definitions()
PUT    /api/contracts/{table}/description → contract_svc.set_table_description()
POST   /api/contracts/{table}/sql-rules → contract_svc.add_sql_constraints()
POST   /api/contracts/{table}/tags      → contract_svc.add_tags()
PUT    /api/contracts/{table}/columns/{col}/classification → contract_svc.override_classification()
PUT    /api/contracts/{table}           → contract_svc.apply_business_update()
POST   /api/contracts/{table}/import    → contract_svc.apply_business_yaml()
POST   /api/contracts/import-bulk       → contract_svc.import_business_file()
```

## CLI command mapping (for reference)

```bash
# ── Scan ───────────────────────────────────────────
redibis scan --file data.csv --table telecom.customers --equation balanced
redibis scan --file data.csv --table telecom.customers --quality-only
redibis scan --file data.csv --table telecom.customers --no-ge-docs

# ── Browse ─────────────────────────────────────────
redibis list                                    # list all tables
redibis show telecom.customers                  # show active contract
redibis show telecom.customers --yaml           # as YAML
redibis show telecom.customers --format sodacl  # export to Soda
redibis history telecom.customers               # audit trail
redibis diff telecom.customers UUID_A UUID_B    # compare versions
redibis runs                                    # list recent runs
redibis runs telecom.customers                  # runs for a table
redibis run telecom.customers 2026-05-13_10-00  # specific run

# ── Update ─────────────────────────────────────────
redibis update telecom.customers --description "Customer master data"
redibis update telecom.customers --column phone --desc "Primary phone (E.164)"
redibis update telecom.customers --tag regulated --tag gdpr
redibis update telecom.customers --business-yaml definitions.yaml
redibis import-business glossary.yaml           # bulk import
redibis override telecom.customers city internal  # classification override
```

---

## Data flow for each workflow

### Workflow A — Quality-only scan

```
CSV → ScanService.scan_csv(config=ScanConfig(run_pii=False))
    → QualityProfiler → QualityGatekeeper → QualityContractWriter
    → ContractStore.upsert(workflow="quality")
    → ScanResult with quality artifacts only
```

### Workflow B — Full scan (quality + PII)

```
CSV → ScanService.scan_csv(config=ScanConfig(run_pii=True))
    → QualityProfiler → QualityGatekeeper → QualityContractWriter
    → PIIDetector → EquationEngine → PIIContractWriter
    → ContractStore.upsert(workflow="quality")
    → ContractStore.upsert(workflow="pii")
    → ScanResult with all artifacts
```

### Workflow C — Business enrichment

```
YAML → ContractService.apply_business_yaml(table, path)
     → ContractStore.upsert(workflow="business")
     → UpsertResult
```

### Read path

```
GUI/CLI → BrowseService.get_contract(table)
        → ContractStore.get_active(table)
        → Returns the MERGED contract with quality + PII + business
```

---

## Dependencies between services and core modules

```python
# ScanService uses:
from redibis.quality.sampling import PandasTableSampler
from redibis.quality.profiler import QualityProfiler
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.quality.contract_writer import QualityContractWriter
from redibis.pii.contract_writer import PIIContractWriter
from redibis.pii.equations import decide_pii
from redibis.pii.thresholds import Thresholds
from redibis.store.contract_store import ContractStore
from redibis.store.run_output_writer import RunOutputWriter

# BrowseService uses:
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import StorageBackend
from redibis.contracts.exporter import export_contract

# ContractService uses:
from redibis.store.contract_store import ContractStore
from redibis.contracts.mapper import sql_rule_to_odcs
```

No service imports another service. No circular dependencies.
