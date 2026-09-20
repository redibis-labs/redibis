# redibis Quality Module — Developer Guide

Comprehensive reference for `redibis.quality.profiler.QualityProfiler` and
`redibis.quality.gatekeeper.QualityGatekeeper`.

---

## Overview

The quality module has two main classes that work together:

**`QualityProfiler`** — the profiling sandbox. Runs entirely in memory.
Produces draft expectations, Arabic detection, and column triage signals.
Generates HTML reports for interactive rule curation.

**`QualityGatekeeper`** — the enforcement gate. Runs expectation suites
against DataFrames (Pandas or Spark), generates GE Data Docs HTML reports,
and exports quality-only ODCS v3.0.1 contracts.

The typical workflow is: profile → curate → enforce → export.

```
  QualityProfiler              QualityGatekeeper
  ┌───────────────┐            ┌──────────────────────┐
  │ run_assistant  │ ─curated─→│ merge_expectations    │
  │ review         │ rules     │ add_*_quality_checks  │
  │ drop_rules     │           │ add_gx_expectation    │
  │ export HTML    │           │ run_tests             │
  └───────────────┘            │ copy_data_docs_to     │
                               │ export_quality_contract│
                               └──────────────────────┘
```

---

## Installation

```bash
pip install redibis[ge]           # Great Expectations support
pip install redibis[ge,dev]       # + pytest, numpy for testing
```

---

## Tutorial 1 — Profile a DataFrame and Review Draft Rules

This tutorial covers: creating a profiler, running the GE data assistant,
reviewing draft expectations, and curating them interactively.

### Step 1: Create a profiler from your DataFrame

The profiler accepts both Pandas and Spark DataFrames. It runs entirely
in memory — no filesystem writes, no GE project directory needed.

```python
import pandas as pd
from redibis.quality.profiler import QualityProfiler

# Pandas DataFrame
df = pd.read_csv("telecom_customers.csv")
profiler = QualityProfiler(df, dataset_name="telecom_customers")

# OR from a Spark DataFrame
# spark_df = spark.table("telecom.customers").limit(5000)
# profiler = QualityProfiler(spark_df, dataset_name="telecom_customers")
```

### Step 2: Run the GE OnboardingDataAssistant

This auto-generates ~50 structural expectations covering all columns
(null rates, type inference, cardinality, value ranges, etc.).

```python
profiler.run_assistant(exclude_columns=["dt", "partition_key"])
# Output: "Found 52 potential rules."
```

### Step 3: Review the draft rules

Three review methods, from simplest to richest:

**Console review** — prints a numbered list to the terminal:

```python
profiler.review()
# Output:
# [  0]  customer_id               expect_column_values_to_not_be_null
#          -> {'mostly': 1.0}
# [  1]  customer_id               expect_column_values_to_be_unique
#          -> {'mostly': 0.95}
# [  2]  phone                     expect_column_values_to_match_regex
#          -> {'regex': '...', 'mostly': 0.8}
# ...
```

**Native GE HTML report** — rendered with draft index numbers in the notes:

```python
profiler.export_html_review(
    output_filename="draft_review.html",
    open_browser=True,
)
# Opens in browser. Each expectation shows its index number
# so you can use drop_rules_by_index() to remove it.
```

**Interactive HTML dashboard** — custom card-based UI with drop/undo buttons:

```python
profiler.export_interactive_review(
    output_filename="interactive_review.html",
    open_browser=True,
)
# Opens a Tailwind-styled dashboard where you can:
#   - Click "Drop" on any rule to mark it for removal
#   - Click "Code" to see the Python code to reproduce the expectation
#   - Copy the generated cleanup script at the bottom
#   - Paste into your notebook: profiler.drop_rules_by_index([3, 7, 12])
```

### Step 4: Curate — remove noisy rules

After reviewing, remove rules that are too noisy or irrelevant:

```python
# By index (from the review output)
profiler.drop_rules_by_index([3, 7, 12, 15])

# By column name
profiler.remove_rules(columns=["partition_key", "dt"])

# By expectation type
profiler.remove_rules(rule_types=["expect_column_max_to_be_between"])
```

After curating, the remaining rules are in `profiler.expectations`.
These are the rules you'll hand to the gatekeeper.

---

## Tutorial 2 — Run Quality Checks In Memory

This tutorial covers: creating a gatekeeper in memory mode, adding
expectations, running the suite, and inspecting results. No filesystem
writes, no Data Docs HTML — pure in-memory validation.

### Step 1: Create a gatekeeper and attach a DataFrame

```python
from redibis.quality.gatekeeper import QualityGatekeeper

qa = QualityGatekeeper(
    suite_name = "telecom_quality",
    in_memory  = True,             # no filesystem, no Data Docs
)
qa.attach_dataframe(df, dataset_name="telecom_customers")
```

### Step 2: Add expectations

Four ways to add expectations, from high-level to low-level:

**From profiler (recommended)** — merges the curated draft rules:

```python
qa.merge_expectations(profiler.expectations)
```

**Table-level checks:**

```python
qa.add_table_quality_checks(
    min_rows         = 100,
    max_rows         = 1_000_000,
    expected_columns = ["customer_id", "phone", "email", "city"],
)
```

**Column-level checks:**

```python
qa.add_column_quality_checks("customer_id", not_null=True, unique=True)
qa.add_column_quality_checks("phone",       not_null=True, mostly=0.99)
qa.add_column_quality_checks("email",       not_null=True, mostly=0.95)

qa.add_percentage_quality_check(
    column    = "account_type",
    value_set = ["prepaid", "postpaid", "enterprise"],
    mostly    = 0.95,
)
```

**Any GE expectation (escape hatch):**

```python
qa.add_gx_expectation(
    "expect_column_values_to_match_regex",
    column = "phone",
    regex  = r"^\+20\d{10}$",
    mostly = 0.90,
)

qa.add_gx_expectation(
    "expect_column_value_lengths_to_be_between",
    column    = "national_id",
    min_value = 14,
    max_value = 14,
)

qa.add_gx_expectation(
    "expect_column_values_to_be_between",
    column    = "balance",
    min_value = 0,
    max_value = 100000,
)
```

### Step 3: Run the suite

```python
results = qa.run_tests(
    stage         = "quality_check",
    generate_docs = False,          # no docs in memory mode
)

if results.success:
    print("All quality checks passed!")
else:
    print("Some checks failed — inspect the results.")
```

### Step 4: Inspect results

```python
# List all expectations in the suite
for exp in qa.list_expectations():
    print(f"{exp['expectation_type']} on {exp['kwargs'].get('column', 'TABLE')}")
```

---

## Tutorial 3 — Run Quality Checks with Full GE Data Docs Report

This tutorial covers: file-backed mode, running the suite with HTML
report generation, and copying the Data Docs to a target directory.

### Step 1: Create a file-backed gatekeeper

```python
from pathlib import Path
from redibis.quality.gatekeeper import QualityGatekeeper

qa = QualityGatekeeper(
    suite_name       = "telecom_quality",
    in_memory        = False,                       # file-backed
    context_root_dir = Path("./ge_project"),         # GE writes here
)
qa.attach_dataframe(df, dataset_name="telecom_customers")
```

### Step 2: Add expectations and run

```python
# Merge curated rules from the profiler
qa.merge_expectations(profiler.expectations)

# Add custom checks on top
qa.add_column_quality_checks("phone", not_null=True)

# Run with Data Docs generation
results = qa.run_tests(
    stage         = "quality_scan",
    generate_docs = True,           # builds HTML report
)
```

### Step 3: Copy Data Docs to a target directory

The GE Data Docs are a full HTML site (index.html + CSS + JS + assets).
`copy_data_docs_to()` copies the entire `local_site/` folder to your
target directory so the report renders correctly when opened.

```python
ge_report_dir = Path("./run_output/ge_report")
index_html = qa.copy_data_docs_to(ge_report_dir)
print(f"Open in browser: file://{index_html.resolve()}")

# The copied folder contains:
#   ge_report/
#   ├── index.html          ← main report page
#   ├── static/             ← CSS, JS, fonts
#   └── expectations/       ← per-suite detail pages
```

### Step 4: Upload the report to S3 (optional)

```python
from redibis.store.storage_backend import get_backend
from redibis.store.run_output_writer import RunOutputWriter

backend = get_backend(mode="auto")
writer = RunOutputWriter(
    backend  = backend,
    bucket   = "pii-reports",
    workflow = "quality",
    table    = "telecom.customers",
    run_id   = "2026-05-11_10-00-00",
)
writer.write_folder("ge_report", ge_report_dir)
```

---

## Tutorial 4 — SQL Validation

The gatekeeper supports SQL-based quality checks. For Pandas DataFrames,
it uses DuckDB (`pip install duckdb`). For Spark DataFrames, it uses
native SparkSQL.

```python
# Pandas + DuckDB
qa.enforce_sql_rule(
    query="SELECT * FROM pipeline_data WHERE balance < 0",
    expected_violation_count=0,
)

qa.enforce_sql_rule(
    query="""
        SELECT customer_id, COUNT(*) as cnt
        FROM pipeline_data
        GROUP BY customer_id
        HAVING cnt > 1
    """,
    expected_violation_count=0,
)

# Spark (same interface)
# qa.enforce_sql_rule(
#     query="SELECT * FROM pipeline_data WHERE balance < 0",
#     expected_violation_count=0,
# )
```

If the violation count exceeds `expected_violation_count`, a `ValueError`
is raised with the query and counts.

---

## Tutorial 5 — Export a Quality-Only ODCS Contract

After running quality checks, export a partial ODCS v3.0.1 contract
containing only quality blocks. No PII, no classification tags, no
pii_summary.

```python
contract = qa.export_quality_contract(
    database_name = "telecom",
    table_name    = "customers",
)

# The contract dict looks like:
# {
#     "apiVersion": "v3.0.1",
#     "kind": "DataContract",
#     "name": "telecom_customers_contract",
#     "database_name": "telecom",
#     "table_name": "customers",
#     "schema": [{
#         "name": "telecom_customers",
#         "physicalName": "telecom.customers",
#         "properties": [
#             {
#                 "name": "phone",
#                 "logicalType": "string",
#                 "required": true,
#                 "quality": [
#                     {
#                         "rule": "expect_column_values_to_not_be_null",
#                         "engine": "greatExpectations",
#                         "implementation": "expect_column_values_to_not_be_null(column='phone')"
#                     },
#                     {
#                         "rule": "expect_column_values_to_match_regex",
#                         "engine": "greatExpectations",
#                         "implementation": "expect_column_values_to_match_regex(column='phone', regex='^\\+20\\d{10}$', mostly=0.9)"
#                     }
#                 ]
#             },
#             ...
#         ]
#     }]
# }
```

### Write to file

```python
contract = qa.export_quality_contract(
    database_name = "telecom",
    table_name    = "customers",
    output_path   = "./output/quality_contract.yaml",
)
```

### Push to the contract store (smart upsert)

```python
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

backend = LocalBackend("./dev_storage")
store   = ContractStore(backend, bucket="contracts")

result = store.upsert(
    partial  = contract,
    table    = "telecom.customers",
    workflow = "quality",
    run_id   = "2026-05-11_10-00-00",
)
print(f"Contract upserted: v{result.version_after}")
```

---

## Tutorial 6 — Arabic-Aware Profiling

The profiler detects Arabic content in string columns and flags them.
This is relevant for both quality (bilingual data validation) and PII
(Arabic-aware recognizers).

```python
profiler = QualityProfiler(df, dataset_name="telecom_customers")
profiler.run_assistant()
profiler.profile_arabic_presence(threshold=0.05)

# Check which columns have Arabic content
for col, fraction in profiler.arabic_columns.items():
    if fraction > 0.05:
        print(f"  {col}: {fraction:.1%} Arabic")

# The triage report shows Arabic badges
profiler.export_triage_report(
    output_filename = "triage_report.html",
    open_browser    = True,
)
```

---

## Tutorial 7 — Column Triage for PII Detection

The profiler computes structural signals per column that indicate whether
it's worth scanning for PII. This is used by the PII detector to skip
obviously non-PII columns (like `account_type` or `dt`).

```python
profiler = QualityProfiler(df, dataset_name="telecom_customers")
profiler.run_assistant()
profiler.profile_arabic_presence()
column_profiles = profiler.compute_column_profiles(threshold=0.30)

# Each ColumnProfile has:
for cp in column_profiles:
    flag = "SCAN" if cp.send_to_detector else "skip"
    print(f"  [{flag}] {cp.column:<20} score={cp.triage_score:.2f} "
          f"card={cp.cardinality_ratio:.3f} len={cp.avg_value_length:.1f} "
          f"ar={cp.arabic_fraction:.0%}")

# Only pass send_to_detector=True columns to the PII detector
scan_columns = [cp for cp in column_profiles if cp.send_to_detector]
```

Triage weights (calibrated, locked):

| Signal | Weight | Description |
|--------|--------|-------------|
| Name hint | 0.40 | Token matching against known PII column name patterns |
| Cardinality ratio | 0.20 | High uniqueness suggests identifiers |
| Average length | 0.20 | PII values tend to be 8–40 characters |
| Null rate | 0.10 | Penalty for very high null rates |
| Arabic fraction | 0.10 | Arabic content signals bilingual PII |

---

## Tutorial 8 — End-to-End Quality Workflow (No PII)

Complete standalone quality workflow — from sampling through contract output.

```python
from redibis.quality.sampling import PandasTableSampler, SamplingConfig
from redibis.quality.profiler import QualityProfiler
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from pathlib import Path

# 1. Sample (Pandas mode for development)
df = PandasTableSampler(SamplingConfig(
    strategy="fixed_rows", fixed_row_count=5000
)).from_dataframe(full_df)

# 2. Profile
profiler = QualityProfiler(df, dataset_name="telecom_customers")
profiler.run_assistant(exclude_columns=["dt"])
profiler.profile_arabic_presence()

# 3. Review and curate
profiler.export_interactive_review("review.html", open_browser=True)
# ... user opens browser, marks noisy rules, copies cleanup script ...
profiler.drop_rules_by_index([3, 7, 12])    # paste from the review UI

# 4. Enforce via gatekeeper
qa = QualityGatekeeper(
    suite_name       = "telecom_quality",
    in_memory        = False,
    context_root_dir = Path("./ge_project"),
)
qa.attach_dataframe(df, dataset_name="telecom_customers")
qa.merge_expectations(profiler.expectations)
qa.add_table_quality_checks(min_rows=100)
qa.add_column_quality_checks("customer_id", not_null=True, unique=True)

# 5. Run and generate report
results = qa.run_tests(stage="quality_scan", generate_docs=True)

# 6. Copy Data Docs
report_dir = Path("./run_output/ge_report")
qa.copy_data_docs_to(report_dir)

# 7. Export quality contract
contract = qa.export_quality_contract(
    database_name = "telecom",
    table_name    = "customers",
    output_path   = "./run_output/quality_contract.yaml",
)

# 8. Push to contract store (optional)
backend = LocalBackend("./dev_storage")
store   = ContractStore(backend, bucket="contracts")
result  = store.upsert(contract, table="telecom.customers", workflow="quality")
print(f"Done. Contract v{result.version_after}")
```

---

## Complete Method Reference

### QualityProfiler

| Method | Purpose |
|--------|---------|
| `__init__(df, dataset_name)` | Create profiler. Accepts Pandas or Spark DataFrame. |
| `run_assistant(exclude_columns=None)` | Run GE OnboardingDataAssistant; populates `self.expectations`. |
| `profile_arabic_presence(columns=None, threshold=0.05)` | Detect Arabic content per string column; populates `self.arabic_columns`. |
| `compute_column_profiles(threshold=0.30)` | Compute structural triage signals per column; returns `list[ColumnProfile]`. |
| `review()` | Print numbered list of draft expectations to console. |
| `remove_rules(columns=None, rule_types=None)` | Remove rules by column name or expectation type. |
| `drop_rules_by_index(indices_to_drop)` | Remove specific rules by their zero-based index. |
| `export_html_review(output_filename, open_browser)` | Generate native GE HTML report with draft index numbers. |
| `export_interactive_review(output_filename, open_browser)` | Generate custom interactive HTML dashboard for rule curation. |
| `export_triage_report(output_filename, open_browser)` | Generate HTML table of triage scores per column. |

**Key attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `expectations` | `list[ExpectationConfiguration]` | The current list of curated expectations. |
| `arabic_columns` | `dict[str, float]` | Column → Arabic character fraction. |
| `triage_signals` | `list[ColumnProfile]` | Property returning the last computed triage signals. |

### QualityGatekeeper

| Method | Purpose |
|--------|---------|
| `__init__(suite_name, in_memory, context_root_dir)` | Create gatekeeper. `in_memory=True` for cluster, `False` for local with Data Docs. |
| `attach_dataframe(df, dataset_name)` | Attach a Pandas or Spark DataFrame. Required before any checks. |
| `merge_expectations(new_expectations)` | Merge profiler-generated expectations into the suite. |
| `add_table_quality_checks(min_rows, max_rows, expected_columns)` | Table-level: row count bounds and schema validation. |
| `add_column_quality_checks(column, not_null, unique, type_list, mostly)` | Column-level quality checks with tolerance. |
| `add_percentage_quality_check(column, value_set, mostly)` | Categorical set membership check. |
| `add_gx_expectation(expectation_name, **kwargs)` | Dynamic bridge to ANY built-in GE expectation by name. |
| `enforce_sql_rule(query, expected_violation_count)` | SQL validation (DuckDB for Pandas, SparkSQL for Spark). |
| `run_data_assistant(exclude_columns=None)` | Run OnboardingDataAssistant directly inside the gatekeeper. |
| `run_tests(stage, generate_docs, open_browser)` | Execute the full expectation suite. Returns GE results. |
| `copy_data_docs_to(target_dir, site_name)` | Copy full Data Docs folder (Option B). Requires `in_memory=False`. |
| `export_quality_contract(database_name, table_name, output_path)` | Export quality-only ODCS v3.0.1 partial contract. |
| `export_to_odcs(model_name, output_path)` | Legacy export (backward compat). Use `export_quality_contract` for new code. |
| `compute_column_profiles(threshold)` | Quick inline triage without a separate QualityProfiler instance. |
| `list_expectations()` | List all expectations in the suite as dicts. |
| `get_quality_results()` | Return last run's validation results. |

---

## When to Use Each Mode

| Scenario | Mode | Data Docs? | Contract? |
|----------|------|------------|-----------|
| Quick in-notebook validation | `in_memory=True` | No | Yes (dict) |
| Local development with full reports | `in_memory=False` | Yes | Yes (dict + YAML) |
| CI/CD pipeline quality gate | `in_memory=True` | No | Yes → upsert to store |
| Airflow scheduled **discovery** scan | `in_memory=False` | Yes → upload to S3 | Yes → upsert to store |
| Airflow scheduled **quality monitor** (validate-only) | via `redibis quality-monitor` | Yes → `monitor/` bucket | **No** contract write |
| PII detection workflow | `in_memory=False` | Yes (via PIIQualityBridge) | PIIContractWriter handles |

---

## Continuous quality monitoring

After rules are approved and merged, use **`redibis quality-monitor`** (not another
discovery scan) to re-validate the frozen contract. The name is deliberately
narrow: this service validates quality expectations only, never PII or business
definitions. `redibis monitor …` still works as a deprecated alias.

```bash
redibis quality-monitor run telecom.customers --sample ./sample.csv --json
redibis quality-monitor batch --all-contracts --json
redibis quality-monitor export telecom.customers -o ./packages
redibis quality-monitor export telecom.customers --python-file ./edited_quality.py -o ./packages
```

Library entry point (validate-only, no `ContractStore.upsert`):

```python
from redibis.quality.contract_validate import validate_contract_quality

result, qa, raw = validate_contract_quality(df, contract, table="telecom.customers")
print(result.status, result.rules_passed, "/", result.rules_total)
```

Service orchestration (artifacts + configured result-sink publish): `redibis.services.continuous_quality.ContinuousQualityService`.

### Portable automation: full code export, canonical results, pluggable sinks

Rules aren't locked to Redibis' own scheduler. `redibis quality-monitor export`,
the web **Download monitor package** button, and the web **Copy Jupyter Code**
action all render the same authoritative, complete Python program
(`redibis/quality/ge_codegen.py::render_full_quality_program`, resolved through
`redibis/services/quality_code.py`) — paste it into Jupyter, run it from cron,
Airflow, Dagster, or any other open-source scheduler; Redibis is only ever a
`pip install redibis[ge,spark]` dependency at runtime, never a required
orchestrator.

The program is **Spark-first**: it validates a Spark DataFrame in place with no
`toPandas()`, no row cap, and no driver-side collect, because a quality verdict
needs a real unit of data (a full business-day transaction partition), not a
5,000-row sample. It takes a catalog table or a CSV/Parquet/Delta path plus an
optional Spark partition predicate. `--engine pandas` renders a small-file
local-development variant.

Every rule and parameter is embedded as a readable Python literal in an inline
`RULES = [...]` block, so the copied program is genuinely self-contained: editing
the program *is* editing the rules, and nothing Redibis generated is read at
runtime. The rules-only `ge_paste.py` fragment remains in the package as a
compatibility artifact for the UI paste panel.

Round-tripping is safe and one-way. An edited program can be brought back through
`--python-file` on the CLI, or the paste panel / `📄 load .py file` picker in the
web UI. Both use the AST + literal-only parser
(`redibis/contracts/rule_code_parser.py`), which now also recognizes the inline
`RULES = [...]` literal. Imported code yields **proposed** rules; it is never
imported, compiled, or executed, and contract writes still require the existing
approve → merge flow.

Run results are emitted as `redibis.io/quality/v1alpha1` — a versioned,
backend-neutral `QualityRun` (`redibis/quality/schema.py`) alongside the legacy
`quality_results.json`, so any consumer can be built against a stable schema
instead of Redibis' internal GE-shaped result dict. Publishing that result to a
dashboard is a strategy-pattern **result sink**
(`redibis/quality/sinks/registry.py`) — OpenMetadata ships built-in; a Soda,
DataHub, or OpenLineage sink is a new registered class, not a fork of the
monitor service.

Full CLI + package layout: [cli/monitor.md](cli/monitor.md) · architecture: [QUALITY_SCAN_ALL_WAYS.md](QUALITY_SCAN_ALL_WAYS.md) §6.

## Integration with PII Detection (via PIIQualityBridge)

When you need PII expectations alongside quality expectations in the same
GE report, use the bridge from the PII package:

```python
from redibis.pii.quality_bridge import PIIQualityBridge

# After running quality checks...
bridge = PIIQualityBridge(qa)
for detection in pii_detections:
    bridge.register(detection)

# Now the GE suite has BOTH quality + PII expectations
results = qa.run_tests(generate_docs=True)
qa.copy_data_docs_to("./ge_report")  # report shows both types
```

The bridge is one-way: `redibis.pii` writes into `redibis.quality`.
The quality package never imports from `redibis.pii`.

---

## Integration with Contract Store

The quality contract is a partial ODCS document. When pushed through
`ContractStore.upsert()`, it merges with any existing PII or business
contracts for the same table:

```python
# Quality workflow produces quality-only partial
quality_contract = qa.export_quality_contract("telecom", "customers")

# PII workflow produces PII-only partial
pii_contract = pii_writer.build()

# Both are upserted to the same table
store.upsert(quality_contract, table="telecom.customers", workflow="quality")
store.upsert(pii_contract,     table="telecom.customers", workflow="pii")

# The active contract now has BOTH quality blocks AND pii blocks
active = store.get_active("telecom.customers")
# active["schema"][0]["properties"][0]["quality"] → from quality workflow
# active["schema"][0]["properties"][0]["pii"]     → from PII workflow
# active["schema"][0]["properties"][0]["tags"]    → union of both workflows
```

---

## Spark DataFrame Support

Both classes accept Spark DataFrames. The main difference is in how the
GE datasource is created internally (Spark vs Pandas), which is handled
automatically by `attach_dataframe()`.

```python
# Works identically with Spark
spark_df = spark.table("telecom.customers").limit(5000)

profiler = QualityProfiler(spark_df, dataset_name="telecom_customers")
profiler.run_assistant()

qa = QualityGatekeeper(suite_name="quality", in_memory=False)
qa.attach_dataframe(spark_df, dataset_name="telecom_customers")
qa.merge_expectations(profiler.expectations)
results = qa.run_tests(generate_docs=True)
```

For SQL validation, Spark DataFrames use native SparkSQL instead of DuckDB:

```python
qa.enforce_sql_rule(
    query="SELECT * FROM pipeline_data WHERE balance < 0",
    expected_violation_count=0,
)
# Spark: uses createOrReplaceTempView("pipeline_data") + spark.sql()
# Pandas: uses DuckDB's pipeline_data variable binding
```

---

## HTML Reports Quick Reference

| Report type | Generated by | Description |
|-------------|-------------|-------------|
| GE Data Docs (full) | `qa.run_tests(generate_docs=True)` + `qa.copy_data_docs_to()` | Full GE HTML site with all expectations, results, stats |
| Native HTML review | `profiler.export_html_review()` | GE-rendered page with draft index numbers in notes |
| Interactive review | `profiler.export_interactive_review()` | Custom card UI with drop/undo/copy-code buttons |
| Triage report | `profiler.export_triage_report()` | Table of structural triage scores per column |

---

## FAQ

**Q: Can I use the gatekeeper without the profiler?**
Yes. You can add expectations manually via `add_*_quality_checks()` and
`add_gx_expectation()` without ever running a profiler. The profiler is
a convenience for auto-generating draft rules.

**Q: Can I run quality checks without producing a contract?**
Yes. Just call `run_tests()` and inspect `results.success`. The contract
export is a separate, optional step.

**Q: Can I run quality checks in memory and still get an ODCS contract?**
Yes. `export_quality_contract()` works in both modes. You just won't get
Data Docs HTML in memory mode.

**Q: Can I add custom metadata to expectations?**
Yes, via the `meta` kwarg on `add_gx_expectation()`:

```python
qa.add_gx_expectation(
    "expect_column_values_to_not_be_null",
    column="phone",
    mostly=0.99,
    meta={"notes": {"content": "Business rule: phone is mandatory for postpaid"}},
)
```

**Q: What GE version is required?**
Great Expectations 0.17.x. The API calls (`sources.add_or_update_pandas`,
`assistants.onboarding.run`, etc.) are 0.17-specific.

**Q: Can I use this with Apache Iceberg tables?**
Yes, via Spark. Read the Iceberg table into a Spark DataFrame and pass it
to the profiler/gatekeeper as shown in the Spark section above. The quality
contract that gets produced is table-agnostic — it describes the columns
and rules, not the storage format.
