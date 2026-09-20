# Quality scan in redibis — every way to run it, tune it, and automate it (with code)

Companion to `docs/PROFILING_ALL_WAYS.md`. Covers: the ways to run a quality scan, how to **remove
rule types for PII columns**, how to **drop just min/max (range) checks for a subset of columns**,
how to **add a custom SQL quality check**, how to **add custom GE rules**, and how to **automate
schema/quality daily with per-table reports** (GE reports + custom reports).

> Grounded in the current source. The dev shell was unavailable while writing, so snippets are
> accurate-to-the-API — smoke them in your venv.

---

## 0. Mental model

Quality has three layers:
1. **Generation** — profiling proposes candidate expectations (`ProfileResult.suggested_rules`), and
   the **GE gatekeeper** runs them (`run_quality_phase` / `QualityGatekeeper`), producing data docs.
2. **The contract** — accepted rules live as ODCS `quality` entries on the schema (via
   `ScanContractWriter` → `ContractStore.upsert`). Each rule has a stable `rule_id`, a `column`, and
   a GE `expectation_type` + `kwargs`.
3. **The decision overlay** — `_meta/quality_decisions/{table}.json` (`QualityDecisionStore`): per-rule
   **suppress** / **manual** decisions enforced on every upsert. This is how you *remove* or *add*
   rules **without** hand-editing the contract (invariant #1: `upsert` is the only writer).

| Action | API | Overlay/Writer |
|---|---|---|
| See rules | `ContractStore.get_quality_view(table)` | reads contract + decisions |
| Remove a rule | `suppress_quality_rule(table, rule_id)` | quality decision = `suppressed` |
| Remove all | `suppress_all_quality_rules(table)` | bulk suppress |
| Restore | `restore_quality_rule(table, rule_id)` | drops the decision |
| Add a rule | `add_manual_quality_rule(table, rule_payload)` | quality decision = `manual` |
| Add SQL rule | `ContractService.add_sql_constraints(...)` | ODCS DataQuality `type="sql"` |

---

## 1. Ways to RUN a quality scan

```python
import pandas as pd
from redibis.scan import QualityScan, ScanConfig
df = pd.read_csv("telecom_customers.csv")

# (a) Facade — profile + GE gatekeeper, writes data docs when run_dir is given
res = QualityScan(ScanConfig(table="telecom.customers")).run(df, run_dir="./reports")
print(res.profile.suggested_rules)          # candidate expectations
print(res.artifacts.get("interactive_review"))  # GE-backed quality report (HTML)

# (b) Engine orchestrator
from redibis.scan import Scan
run = Scan(ScanConfig(table="telecom.customers", run_quality=True)).run(df, run_dir="./reports")

# (c) Shared phase directly (what ScanService + agents call)
from redibis.scan.quality_phase import run_quality_phase
from redibis.services.scan_service import to_scan_config
from redibis.config import RedibisConfig
cfg = to_scan_config(RedibisConfig.default(), table="telecom.customers")
quality_contract, qa, results, stats = run_quality_phase(df, cfg, profile, db_name="telecom",
                                                         tbl_name="customers", run_dir="./reports")
print(stats)   # {total, passed, failed}

# (d) CLI
#   redibis scan --table telecom.customers --csv data.csv --scan-types quality
#   redibis --help    # exact flags
```

The GE gatekeeper writes **data docs** (`copy_data_docs_to`) — the `ge_report/` + `interactive_review.html`
artifacts (invariant #9). These are your GE reports.

---

## 2. Remove rule TYPES for PII columns

PII columns shouldn't carry leaky/meaningless checks (e.g. a value-set, regex, or min/max on an
SSN). The PII columns are already known via the **PII decision overlay**
(`ContractStore.get_pii_decisions(table)` / `get_pii_view`). Drive suppression from it:

```python
from redibis.store.contract_store import ContractStore
store = ContractStore(...)

LEAKY_FOR_PII = {
    "expect_column_values_to_be_in_set",      # value set leaks/over-fits PII
    "expect_column_values_to_be_between",     # min/max range — meaningless for IDs
    "expect_column_values_to_match_regex",    # may encode the PII pattern
    "expect_column_unique_value_count_to_be_between",
}

pii_view = store.get_pii_view("telecom.customers")
pii_cols = {c["column"] for c in pii_view.get("columns", []) if c.get("detected") or c.get("flag")}

for r in store.get_quality_view("telecom.customers")["rules"]:
    etype = r.get("expectation_type") or (r.get("engine_hint") or {}).get("expectation_type")
    if r.get("column") in pii_cols and etype in LEAKY_FOR_PII:
        store.suppress_quality_rule("telecom.customers", r["rule_id"],
                                    column=r.get("column"), decided_by="policy:pii")
```

Keep the **safe** checks on PII columns (e.g. `not_null`, `unique`) — only the value-revealing types
are removed. This is policy-as-code; run it after each scan (or wire it into the scan post-step). The
suppression is an overlay, so the contract stays the single source of truth and a re-scan won't
re-introduce the removed rules.

---

## 3. Remove just min/max (range) checks for a SUBSET of columns

The min/max check is `expect_column_values_to_be_between`. Suppress it for chosen columns:

```python
TARGET_COLS = {"amount", "tenure_months"}     # the subset you want without range checks
for r in store.get_quality_view("telecom.customers")["rules"]:
    etype = r.get("expectation_type") or (r.get("engine_hint") or {}).get("expectation_type")
    if etype == "expect_column_values_to_be_between" and r.get("column") in TARGET_COLS:
        store.suppress_quality_rule("telecom.customers", r["rule_id"], column=r["column"],
                                    decided_by="analyst")
```
REST equivalent: `GET /api/contracts/{table}/quality-view` → for each matching rule
`POST /api/contracts/{table}/quality-decisions/{rule_id}/suppress`. Restore later with
`restore_quality_rule(table, rule_id)` (or `.../{rule_id}/restore`). `suppress_all_quality_rules(table)`
clears the lot.

---

## 4. Add a CUSTOM SQL quality check

SQL business rules become ODCS DataQuality entries with `type="sql"` — first-class, catalog-visible.

```python
from redibis.services.contract_service import ContractService, SQLConstraint
svc = ContractService(store=store)

svc.add_sql_constraints("telecom.customers", [
    SQLConstraint(
        name="positive_balance",
        sql="SELECT count(*) FROM telecom.customers WHERE balance < 0",  # expect 0
        description="balance must never be negative",
    ),
    SQLConstraint(
        name="valid_status",
        sql="SELECT count(*) FROM telecom.customers WHERE status NOT IN ('active','closed')",
    ),
])
```
Or declaratively via YAML (bulk, reviewable) — `ContractService.apply_business_yaml(table, yaml_text)`:
```yaml
sql_constraints:
  - name: positive_balance
    sql: "SELECT count(*) FROM telecom.customers WHERE balance < 0"
    description: balance must never be negative
  - name: fk_account_exists
    sql: "SELECT count(*) FROM telecom.customers c LEFT JOIN telecom.accounts a ON c.account_id=a.id WHERE a.id IS NULL"
```
These run wherever the catalog/engine executes SQL DataQuality (OpenMetadata/Trino/Spark), and travel
with the contract. (`import_business_file(path)` loads the same YAML from disk.)

---

## 5. Add a CUSTOM GE rule (safe paste, never executed)

Engineers can paste Great Expectations rule *text*; redibis parses it safely (AST parse only — never
`exec`) via `parse_ge_rules`, then you approve it into the contract as a `manual` rule:

```python
from redibis.contracts.rule_code_parser import parse_ge_rules

code = '''
add_gx_expectation(expect_column_values_to_not_be_null, column="email")
add_gx_expectation(expect_column_values_to_match_regex, column="email", regex=r"^[^@]+@[^@]+$")
'''
parsed = parse_ge_rules(code)        # {"rules":[{expectation_type,column,kwargs,...}], "errors":[], "ignored":n}
for rule in parsed["rules"]:
    store.add_manual_quality_rule("telecom.customers", rule_payload=rule,
                                  column=rule.get("kwargs", {}).get("column"),
                                  decided_by="analyst")
```
To **edit** a generated rule: `add_manual_quality_rule(..., source_rule_id=<orig>, suppress_source=True)`
— suppresses the original and adds your replacement in one go. REST: `POST /api/contracts/{table}/quality-decisions/manual`.

---

## 6. Automate daily schema + quality with per-table reports

Continuous monitoring is provided by **`redibis quality-monitor`** (validate-only — never re-discovers or
overwrites contract rules). Full CLI reference: [`docs/cli/monitor.md`](cli/monitor.md).

**Discovery vs monitoring**

1. **Discovery** — profile, curate rules in interactive review, approve, merge into contract.
2. **Monitoring** — re-validate the frozen contract on a schedule or after ETL events.

### 6.1 Recommended: `redibis quality-monitor` (validate-only)

```bash
# One table
redibis quality-monitor run telecom.customers \
  --sample /data/samples/telecom_customers.csv \
  --config /etc/redibis/redibis.yaml \
  --output-dir /var/redibis/reports \
  --json

# All tables with active contracts
redibis quality-monitor batch --all-contracts --config /etc/redibis/redibis.yaml --json

# Export per-table package (rules YAML + runner + Airflow DAGs)
redibis quality-monitor export telecom.customers -o /opt/redibis/packages

# Generate Airflow DAG files
redibis quality-monitor airflow generate --all-contracts -o /opt/airflow/dags/redibis
```

Artifacts land in MinIO under `monitor/{table}/{run_id}/` (`quality_results.json`, the
canonical `quality_run.json`, HTML GE report, `scan_coverage.json`). Results publish to
one or more configured **result sinks** (`quality.publish.sinks`, default `openmetadata`
via the legacy `catalog.push.quality: true` toggle) for history and alerting — see
[canonical schema and result sinks](cli/monitor.md#canonical-schema-and-result-sinks).

### 6.2 Schedule with Airflow

Deploy generated DAGs to `$AIRFLOW_HOME/dags/`:

- `redibis_quality_<table>.py` — daily cron (default `0 6 * * *`)
- `redibis_quality_<table>_event.py` — `schedule=None`; trigger via Airflow REST API after upstream ETL

Set Airflow Variables: `REDIBIS_CONFIG`, `REDIBIS_SAMPLE_ROOT`, `S3_ENDPOINT_URL`.

### 6.3 Alternative: cron + monitor batch

```bash
# /etc/cron.d/redibis-quality
0 6 * * * redibis quality-monitor batch --all-contracts --config /etc/redibis/redibis.yaml \
  >> /var/log/redibis/quality.log 2>&1
```

### 6.4 Legacy: discovery re-scan script (not recommended for production monitoring)

The snippet below re-runs **profiling + discovery** (`QualityScan`), which can suggest new rules.
Use it only when you intentionally want a full re-discovery pass, not for frozen-contract monitoring.

```python
# scripts/daily_quality_discovery.py  (legacy pattern — prefer redibis quality-monitor)
from pathlib import Path
import datetime as dt, json
from redibis.scan import QualityScan, ScanConfig
from redibis.store.contract_store import ContractStore

TABLES = ["telecom.customers", "telecom.accounts", "sales.orders"]
store = ContractStore(...)
day = dt.date.today().isoformat()
out = Path(f"./reports/{day}"); out.mkdir(parents=True, exist_ok=True)
summary = []

for table in TABLES:
    df = load_table_sample(table)                      # your source loader (Spark/JDBC/CSV)
    run_dir = out / table.replace(".", "_")
    res = QualityScan(ScanConfig(table=table)).run(df, run_dir=str(run_dir))   # writes GE data docs
    st = {"table": table, "date": day,
          "passed": getattr(res, "quality_passed", None),
          "total": getattr(res, "quality_expectations", None),
          "ge_report": str(run_dir / "interactive_review.html"),
          "schema_columns": len(store.column_names(table))}
    active_cols = set(store.column_names(table))
    st["schema_added"]  = sorted(set(df.columns) - active_cols)
    st["schema_dropped"] = sorted(active_cols - set(df.columns))
    summary.append(st)

(out / "summary.json").write_text(json.dumps(summary, indent=2))
```

### 6.5 What "schema check" means here

`redibis quality-monitor` compares observed columns to `ContractStore.column_names(table)` →
`schema_added` / `schema_dropped` in `quality_results.json`. For stricter enforcement, add SQL
DataQuality (§4) or compare `logicalType` from profiling against the contract.

---

## 7. REST + CLI quick reference

Dashboard REST routes require a session (cookie + CSRF on writes). See
[`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl). The CLI column
does not.

| Need | REST | CLI / API |
|---|---|---|
| See quality rules | `GET /api/contracts/{table}/quality-view` | `ContractStore.get_quality_view` |
| Suppress a rule | `POST /api/contracts/{table}/quality-decisions/{rule_id}/suppress` | `suppress_quality_rule` |
| Suppress all | `POST /api/contracts/{table}/quality-decisions/suppress-all` | `suppress_all_quality_rules` |
| Restore a rule | `POST /api/contracts/{table}/quality-decisions/{rule_id}/restore` | `restore_quality_rule` |
| Add manual/custom rule | `POST /api/contracts/{table}/quality-decisions/manual` | `add_manual_quality_rule` + `parse_ge_rules` |
| Add SQL constraint | (via business update) | `ContractService.add_sql_constraints` / `apply_business_yaml` |
| Run a quality scan | `POST /api/sessions/{sid}/scan` (web) | `QualityScan(...).run(df)` / `redibis scan` |
| Continuous monitor (no contract write) | — | `redibis quality-monitor run` / `batch` |
| Export monitor package | `POST /api/sessions/{sid}/quality/export-package` | `redibis quality-monitor export` |
| Copy Jupyter-ready quality program | `POST /api/sessions/{sid}/quality/full-code` | `render_quality_program` (`redibis.services.quality_code`) |
| Rebuild a package from edited Python | paste panel / `.py` picker → `POST /api/quality/parse-rules` | `redibis quality-monitor export --python-file` |
| GE reports | `GET /api/sessions/{sid}/artifacts/interactive_review` | `ReportBundle.flush` artifacts |
| OM quality history | `redibis catalog push` (with `catalog.push.quality`) | OpenMetadata table → Quality tab |

---

## 8. Invariants honored
- `ContractStore.upsert` stays the only contract writer; remove/add go through the **quality decision
  overlay** (`suppress`/`manual`), never by hand-editing the contract YAML.
- Suppression/manual decisions live under `_meta/quality_decisions/{table}.json` and are re-applied on
  every upsert, so a re-scan can't silently re-introduce a rule you removed.
- Custom GE text is **parsed, never executed** (`parse_ge_rules` = AST parse + `literal_eval`).
- SQL constraints are declarative ODCS DataQuality (`type="sql"`) — executed by the catalog/engine,
  not by redibis.
