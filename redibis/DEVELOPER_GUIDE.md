# PII Detection Pipeline — Developer Guide

This guide covers how to use `pii_detection` as an importable Python library.
All workflows can be driven entirely from Python code — no CLI required.

---

## Table of Contents

1. [Installation](#1-installation)
2. [Package structure at a glance](#2-package-structure-at-a-glance)
3. [Core concepts and data contracts](#3-core-concepts-and-data-contracts)
4. [Storage setup](#4-storage-setup)
5. [Workflow A — Generate GE expectations and check quality](#5-workflow-a--generate-ge-expectations-and-check-quality)
6. [Curate expectations — add, remove, inspect](#6-curate-expectations--add-remove-inspect)
7. [Run quality checks and view GE reports](#7-run-quality-checks-and-view-ge-reports)
8. [Export to ODCS and view the contract](#8-export-to-odcs-and-view-the-contract)
9. [Workflow B — Run the PII detector and check report and ODCS](#9-workflow-b--run-the-pii-detector-and-check-report-and-odcs)
10. [View the developer UI (profiler dashboard)](#10-view-the-developer-ui-profiler-dashboard)
11. [Merge and compare ODCS contracts](#11-merge-and-compare-odcs-contracts)
12. [Inspect multiple runs and generated artefacts](#12-inspect-multiple-runs-and-generated-artefacts)
13. [Using the equation module standalone](#13-using-the-equation-module-standalone)
14. [Reference — full public API](#14-reference--full-public-api)

---

## 1. Installation

```bash
# Minimal core (storage, models, equation logic only)
pip install pii-detection

# Add GE profiling
pip install "pii-detection[ge]"

# Add NER detection engines (Presidio + GLiNER)
pip install "pii-detection[ner]"

# Add the FastAPI web dashboard
pip install "pii-detection[web]"

# Everything at once
pip install "pii-detection[all]"
```

For development, install from the repo root:

```bash
pip install -e "pii_detection[all,dev]"
```

---

## 2. Package structure at a glance

```
pii_detection/
├── models.py                    # Data contracts: PIIDetection, PIIColumnProfile, RunMetadata
├── decisions/
│   ├── thresholds.py            # Thresholds dataclass, DEFAULT_EQUATION
│   └── equations.py             # decide_pii() — pure decision function
├── layers/
│   ├── sampler.py               # PIISampler, PandasSampler, SamplerConfig
│   ├── ge_profiler.py           # profile_dataframe() — GE triage + Arabic detection
│   ├── ner_detector.py          # detect_pii() — Presidio + GLiNER
│   └── llm_refiner.py           # refine_with_llm() — Milestone 2 stub
├── ge_classes/
│   ├── profiler.py              # DataProfiler — interactive expectation curation
│   └── gatekeeper.py            # DataQualityGatekeeper — run checks, export ODCS
├── catalogs/
│   ├── regex_catalog.py         # 95-pattern telecom regex catalog (pure data)
│   └── recognizer_factory.py    # build_recognizers() — Presidio factory
├── pii_contracts/
│   └── writer.py                # PIIContractWriter — produces PII-only ODCS partial
├── orchestrator/
│   ├── config.py                # PipelineConfig, GEPipelineConfig
│   ├── pipeline.py              # PIIPipeline — Workflow B
│   ├── ge_pipeline.py           # GEPipeline  — Workflow A
│   ├── report_writer.py         # write_pii_run_outputs(), write_ge_run_outputs()
│   └── cli.py                   # pii-scan CLI (calls the pipeline classes above)
└── storage/
    ├── s3_storage.py            # LocalBackend, S3Backend, RunOutputWriter
    ├── contract_store.py        # ContractStoreService — the single writer to pii-contracts
    └── merge.py                 # merge_two_contracts(), IdentityConflictError
```

**Dependency direction (enforced — never reverse):**

```
models ←── decisions ←─┐
  ↑                     ├─ layers ←── orchestrator ←── webapp
  └──────── ge_classes ─┘     ↑
  └──────── storage    ────────┘
```

---

## 3. Core concepts and data contracts

Three dataclasses flow through the pipeline. Import them from the top-level package or directly from `pii_detection.models`:

```python
from pii_detection import PIIDetection, PIIColumnProfile, RunMetadata
```

### `PIIColumnProfile`
Produced by `DataProfiler.pii_triage_signals()`. One per string column.

```python
profile.column            # "phone"
profile.triage_score      # 0.84  (0–1, higher = more likely PII)
profile.send_to_detector  # True  → pass this column to Presidio + GLiNER
profile.arabic_fraction   # 0.0   → fraction of values containing Arabic script
profile.sample_values     # ["01012345678", "01112345678", ...]
```

### `PIIDetection`
Produced by `detect_pii()`, finalised by `decide_pii()`. One per scanned column.

```python
det.column           # "phone"
det.detected         # True  — set by decide_pii(), always False from detector
det.entity_type      # "PHONE_NUMBER"
det.confidence       # 0.91
det.presidio_score   # 0.88  — raw Presidio score
det.gliner_score     # 0.91  — raw GLiNER score
det.classification   # "pii_personal"  (property, derived from entity_type)
det.contributing_engines()  # ["presidio", "gliner"]
```

### `RunMetadata`
Constructed by the orchestrator. Written into the ODCS `pii_summary` block.

```python
from pii_detection import RunMetadata
meta = RunMetadata(
    run_id              = "2026-05-10_14-00-00",
    scan_date           = "2026-05-10T14:00:00Z",
    equation_used       = "balanced",
    table_physical_name = "telecom.customers",
)
```

---

## 4. Storage setup

All workflows need a storage backend and (for contract persistence) a contract store.

### Local filesystem (development)

```python
from pii_detection import LocalBackend, ContractStoreService

backend = LocalBackend("./dev_storage")          # creates the directory
store   = ContractStoreService(backend, bucket="pii-contracts")
```

### MinIO / SeaweedFS / S3 (production)

```python
from pii_detection import S3Config, get_backend, ContractStoreService

cfg     = S3Config(
    endpoint_url          = "http://minio.example.com:9000",
    aws_access_key_id     = "admin",
    aws_secret_access_key = "secret123",
    runs_bucket           = "pii-reports",
    contracts_bucket      = "pii-contracts",
)
backend = get_backend(s3_config=cfg)
store   = ContractStoreService(backend, bucket=cfg.contracts_bucket)
```

### From environment variables

```python
from pii_detection import get_backend, ContractStoreService, S3Config

cfg     = S3Config.from_env()   # reads S3_ENDPOINT_URL, S3_ACCESS_KEY, etc.
backend = get_backend(s3_config=cfg)
store   = ContractStoreService(backend, bucket="pii-contracts")
```

---

## 5. Workflow A — Generate GE expectations and check quality

### 5.1 Load a sample DataFrame

```python
import pandas as pd

# From a local parquet file
df = pd.read_parquet("./samples/telecom_customers.parquet")

# Or build a synthetic sample for testing
df = pd.DataFrame({
    "phone":      ["+201012345678", "+201112345678"],
    "national_id":["29901011234567", "29901021234568"],
    "notes":      ["Called about billing", "مكالمة دعم"],
    "amount":     [100.0, 200.0],
})
```

### 5.2 Create a `DataProfiler` and generate expectations

```python
from pii_detection.ge_classes.profiler import DataProfiler

profiler = DataProfiler(df, dataset_name="telecom_customers")
profiler.run_assistant()          # GE OnboardingDataAssistant — ~50 expectations
profiler.profile_arabic_presence() # detect Arabic-script columns
```

### 5.3 Inspect triage signals

```python
triage = profiler.pii_triage_signals(threshold=0.30)

for signal in triage:
    flag = "SCAN" if signal.send_to_detector else "skip"
    print(f"  [{flag}]  {signal.column:<20}  score={signal.triage_score:.2f}  "
          f"arabic={signal.arabic_fraction:.0%}")
```

Output:
```
  [SCAN]  phone                score=0.88  arabic=0%
  [SCAN]  national_id          score=0.85  arabic=0%
  [SCAN]  notes                score=0.62  arabic=50%
  [skip]  amount               score=0.05  arabic=0%
```

Also accessible as a property after the call:

```python
triage = profiler.triage_signals   # same list, cached
```

---

## 6. Curate expectations — add, remove, inspect

### 6.1 Review all draft expectations

```python
profiler.review()
# Logs a numbered table: [idx]  column_name  expectation_type  -> params
```

### 6.2 Remove by column or expectation type

```python
# Remove all expectations for a non-PII column
profiler.remove_rules(columns=["transaction_id", "created_at"])

# Remove a specific expectation type across all columns
profiler.remove_rules(rule_types=["expect_column_values_to_be_in_set"])
```

### 6.3 Remove by index (from the HTML report)

```python
# Drop specific rules by their Draft Index visible in the HTML report
profiler.drop_rules_by_index([3, 7, 12])
```

### 6.4 Add custom expectations manually

```python
from pii_detection.ge_classes.gatekeeper import DataQualityGatekeeper

qa = DataQualityGatekeeper(
    suite_name = "telecom_customers_quality_suite",
    in_memory  = True,
)
qa.attach_dataframe(df, dataset_name="telecom_customers")
qa.merge_expectations(profiler.expectations)   # start from the generated set

# Add a hand-crafted rule
qa.add_gx_expectation(
    expectation_name = "expect_column_values_to_not_be_null",
    column           = "phone",
)
qa.add_gx_expectation(
    expectation_name = "expect_column_value_lengths_to_be_between",
    column           = "national_id",
    min_value        = 14,
    max_value        = 14,
)
```

---

## 7. Run quality checks and view GE reports

### 7.1 Run in-memory (no filesystem writes)

```python
qa = DataQualityGatekeeper(
    suite_name = "telecom_quality",
    in_memory  = True,
)
qa.attach_dataframe(df, dataset_name="telecom_customers")
qa.merge_expectations(profiler.expectations)

results = qa.run_tests(stage="ge_only", generate_docs=False)

print(f"Total: {results.statistics['evaluated_expectations']}")
print(f"Passed: {results.statistics['successful_expectations']}")
print(f"Failed: {results.statistics['unsuccessful_expectations']}")
```

### 7.2 Run with full GE Data Docs (Option B — complete local_site folder)

```python
from pathlib import Path

qa = DataQualityGatekeeper(
    suite_name       = "telecom_quality",
    in_memory        = False,
    context_root_dir = "./ge_project",
)
qa.attach_dataframe(df, dataset_name="telecom_customers")
qa.merge_expectations(profiler.expectations)

results = qa.run_tests(stage="ge_only", generate_docs=True)

# Copy the full Data Docs folder (preserves CSS/JS — Option B)
qa.copy_data_docs_to("./reports/ge_report")

# Open in browser
import webbrowser
webbrowser.open("./reports/ge_report/index.html")
```

### 7.3 Check individual rule results

```python
for result in results.results:
    status = "PASS" if result.success else "FAIL"
    col    = result.expectation_config.kwargs.get("column", "[table]")
    rule   = result.expectation_config.expectation_type
    print(f"  {status}  {col:<20}  {rule}")
```

---

## 8. Export to ODCS and view the contract

### 8.1 Export a quality-only ODCS partial

```python
qa.export_to_odcs(
    model_name  = "telecom_customers",
    output_path = "./reports/data_contract.yaml",
)

import yaml
with open("./reports/data_contract.yaml") as f:
    contract = yaml.safe_load(f)

print(yaml.dump(contract, allow_unicode=True, default_flow_style=False))
```

### 8.2 Upsert into the contract store

```python
from pii_detection import LocalBackend, ContractStoreService

backend = LocalBackend("./dev_storage")
store   = ContractStoreService(backend, bucket="pii-contracts")

result = store.upsert(
    partial  = contract,
    table    = "telecom.customers",
    workflow = "ge",
    run_id   = "2026-05-10_14-00-00",
)

print(f"Contract UUID: {result.contract_uuid}")
print(f"Version:       {result.version_after}")
print(f"Is new:        {result.is_new}")
```

### 8.3 Read back the active contract

```python
active = store.get_active("telecom.customers")
print(yaml.dump(active, allow_unicode=True, default_flow_style=False))
```

---

## 9. Workflow B — Run the PII detector and check report and ODCS

### 9.1 Programmatic pipeline (recommended for library use)

```python
from pii_detection import (
    PIIPipeline, PipelineConfig,
    LocalBackend, ContractStoreService,
    Thresholds,
)

# 1. Prerequisites: a pre-sampled parquet at ./reports/telecom_customers_sample.parquet
backend = LocalBackend("./dev_storage")
store   = ContractStoreService(backend, bucket="pii-contracts")

config = PipelineConfig(
    table      = "telecom.customers",
    output_dir = "./reports",
    equation   = "balanced",       # "strict" | "balanced" | "lenient"
    thresholds = Thresholds(
        presidio_min               = 0.60,
        gliner_min                 = 0.50,
        very_high_confidence_floor = 0.90,
    ),
    generate_ge_docs = True,
    runs_bucket      = "pii-reports",
)

manifest = PIIPipeline(config, backend, store).run()

print(f"Status:   {manifest['status']}")
print(f"Detected: {manifest['pii_summary']['detected']} / "
      f"{manifest['pii_summary']['scanned']} columns")
```

### 9.2 Step-by-step (for interactive notebooks or fine-grained control)

```python
import pandas as pd
from pii_detection import (
    PIIDetection, Thresholds, decide_pii,
    PIIContractWriter, LocalBackend, ContractStoreService,
)
from pii_detection.ge_classes.profiler import DataProfiler
from pii_detection.layers.ner_detector import detect_pii

df = pd.read_parquet("./samples/telecom_customers.parquet")

# Step 1 — Profile
profiler = DataProfiler(df, dataset_name="telecom_customers")
profiler.run_assistant()
profiler.profile_arabic_presence()
triage = profiler.pii_triage_signals(threshold=0.30)

# Step 2 — Detect (raw scores only — detected is always False here)
raw_detections = detect_pii(df, triage, profiler.arabic_columns)

# Step 3 — Apply equation to get the final verdict
thresholds = Thresholds()
detections = [
    decide_pii(d, equation="balanced", thresholds=thresholds)
    for d in raw_detections
]

# Step 4 — Inspect results
for d in detections:
    if d.detected:
        print(f"  PII  {d.column:<20}  {d.entity_type:<20}  "
              f"confidence={d.confidence:.2f}  engines={d.contributing_engines()}")
    else:
        print(f"  ---  {d.column}")

# Step 5 — Build PII ODCS partial
writer = PIIContractWriter(
    database_name = "telecom",
    table_name    = "customers",
    equation_used = "balanced",
    run_id        = "2026-05-10_14-00-00",
)
writer.add_detections(detections)
pii_partial = writer.build()

# Step 6 — Upsert into contract store
backend = LocalBackend("./dev_storage")
store   = ContractStoreService(backend, bucket="pii-contracts")
result  = store.upsert(pii_partial, table="telecom.customers",
                       workflow="pii", run_id="2026-05-10_14-00-00")

print(f"\nContract version: {result.version_after}")
```

### 9.3 Check the PII ODCS output

```python
import yaml

active = store.get_active("telecom.customers")
schema = active["schema"][0]["properties"]

print(f"\n{'Column':<20} {'Detected':<10} {'Entity':<22} {'Confidence':<12} {'Class'}")
print("-" * 80)
for col in schema:
    pii = col.get("pii", {})
    if pii.get("detected"):
        print(f"{col['name']:<20} {'YES':<10} "
              f"{pii.get('entity_type', ''):<22} "
              f"{pii.get('confidence', 0):.2f}{'':8} "
              f"{col.get('classification', '')}")
    else:
        print(f"{col['name']:<20} {'no':<10}")
```

---

## 10. View the developer UI (profiler dashboard)

The profiler provides two HTML views for interactive expectation curation.

### 10.1 Native GE HTML report (with Draft Index annotations)

```python
# Opens a GE-styled HTML page. Each expectation has its index annotated
# so you can call drop_rules_by_index() after reviewing.
profiler.export_html_review(
    output_filename = "./reports/profiler_draft.html",
    open_browser    = True,
)
```

### 10.2 Interactive DataOps Dashboard

```python
# Custom dashboard with search, filter by column, and one-click
# Python code snippets to add expectations to DataQualityGatekeeper.
profiler.export_interactive_review(
    output_filename = "./reports/interactive_review.html",
    open_browser    = True,
)
```

### 10.3 PII Triage Report

```python
# Colour-coded table: SCAN vs skip, triage scores, Arabic badges, sample values.
profiler.export_triage_report(
    output_filename = "./reports/triage_report.html",
    open_browser    = True,
)
```

### 10.4 Launch the FastAPI web dashboard

The dashboard reads from both storage buckets and shows run cards and contract cards.

```bash
# Local development
USE_LOCAL_STORAGE=true LOCAL_STORAGE_ROOT=./dev_storage \
    uvicorn pii_detection.webapp.backend:app --reload --port 8000
```

Or from Python:

```python
import subprocess
subprocess.Popen([
    "uvicorn", "pii_detection.webapp.backend:app",
    "--reload", "--port", "8000",
])

import webbrowser
webbrowser.open("http://localhost:8000")
```

**Dashboard tabs:**

| Tab | What it shows |
|---|---|
| **Runs** | One card per pipeline run — workflow badge, table, timestamp, PII found, quality checks, artefact links |
| **Contracts** | One card per table — PII columns, quality rules, audit history, Merge button |

---

## 11. Merge and compare ODCS contracts

### 11.1 Merge two partial contracts manually

```python
from pii_detection import merge_two_contracts

ge_partial = {
    "schema": [{"name": "telecom_customers", "physicalName": "telecom.customers",
                "properties": [{"name": "phone", "quality": [{"rule": "not_null"}]}]}]
}
pii_partial = {
    "schema": [{"name": "telecom_customers", "physicalName": "telecom.customers",
                "properties": [{"name": "phone", "pii": {"detected": True,
                                 "entity_type": "PHONE_NUMBER", "confidence": 0.91}}]}]
}

merged = merge_two_contracts(ge_partial, pii_partial, workflow="pii", run_id="r1")

# The merged contract has both quality and pii blocks on the phone column
phone = merged["schema"][0]["properties"][0]
assert "quality" in phone
assert phone["pii"]["detected"] is True
```

### 11.2 Merge three workflows through the contract store

Each workflow writes a partial contract and `ContractStoreService.upsert()` merges automatically:

```python
# Workflow A — GE quality
store.upsert(ge_partial,  table="telecom.customers", workflow="ge",  run_id="r1")

# Workflow B — PII detection
store.upsert(pii_partial, table="telecom.customers", workflow="pii", run_id="r2")

# Workflow C — Business glossary
biz_partial = {
    "schema": [{"name": "telecom_customers", "properties": [{
        "name": "phone",
        "businessName": "Mobile Phone",
        "description":  "E.164 customer phone number",
        "tags":         ["billing", "regulated"],
    }]}]
}
store.upsert(biz_partial, table="telecom.customers", workflow="business", run_id="r3")

# Final merged state — all three workflows' fields present
active = store.get_active("telecom.customers")
phone  = next(p for p in active["schema"][0]["properties"] if p["name"] == "phone")

print(phone["quality"])       # from GE
print(phone["pii"])           # from PII
print(phone["businessName"])  # from Business
print(phone["tags"])          # union: ["billing", "regulated", "pii", "gdpr_personal_data"]
```

### 11.3 Merge rules summary

| Field | Behaviour |
|---|---|
| `contract_uuid`, `database_name`, `table_name` | **Locked** — first-writer-wins. Mismatch raises `IdentityConflictError`. |
| `tags` (per column) | **Set union** — accumulates across all workflows. |
| `quality` block | Replaced wholesale (GE rerun overwrites previous quality). |
| `pii` block | Replaced wholesale (PII rerun overwrites previous detection). |
| All other column fields | Last-writer-wins. |
| `version` | Bumped patch on every upsert (`1.0.0` → `1.0.1`). |
| `provenance` | One entry appended per upsert. |

### 11.4 Identity protection

```python
from pii_detection import IdentityConflictError

# This will raise because contract_uuid is already locked
try:
    store.upsert(
        {"contract_uuid": "different-uuid", "schema": []},
        table="telecom.customers", workflow="ge", run_id="r99"
    )
except IdentityConflictError as e:
    print(f"Blocked: {e}")
```

---

## 12. Inspect multiple runs and generated artefacts

### 12.1 List all runs for a table

```python
from pii_detection import TableHistoryEntry

history = store.get_history("telecom.customers")

print(f"\nAudit history for telecom.customers ({len(history)} runs):\n")
for entry in history:
    print(f"  {entry.timestamp}  {entry.workflow:<10}  "
          f"v{entry.version}  run={entry.run_id}")
```

### 12.2 Retrieve a specific audit snapshot

```python
# history is sorted most-recent first
latest_uuid = history[0].run_uuid
snapshot    = store.get_audit_snapshot("telecom.customers", latest_uuid)

print(yaml.dump(snapshot, allow_unicode=True, default_flow_style=False))
```

### 12.3 Compare two runs

```python
from pii_detection import merge_two_contracts

run1 = store.get_audit_snapshot("telecom.customers", history[1].run_uuid)
run2 = store.get_audit_snapshot("telecom.customers", history[0].run_uuid)

# Show version progression
print(f"Run 1 version: {run1.get('version')}")
print(f"Run 2 version: {run2.get('version')}")

# Show provenance chain
for entry in run2.get("provenance", []):
    print(f"  {entry['timestamp']}  {entry['workflow']}  {entry.get('run_id', '')}")
```

### 12.4 List all artefact keys for a run (using RunOutputWriter pattern)

```python
from pii_detection import LocalBackend

backend = LocalBackend("./dev_storage")
run_prefix = "pii/telecom_customers/2026-05-10_14-00-00"
keys = backend.list_keys("pii-reports", prefix=run_prefix + "/")

for key in keys:
    print(f"  {key.replace(run_prefix + '/', '')}")
# data_contract.yaml
# pii_summary.csv
# pii_detections.json
# sample_preview.parquet
# run_manifest.json
# ge_report/index.html
# ge_report/static/...
```

### 12.5 Read a specific artefact

```python
import json, csv, io

# Run manifest
manifest = json.loads(backend.get_text("pii-reports", f"{run_prefix}/run_manifest.json"))
print(f"Status: {manifest['status']}")
print(f"Timing: {manifest['layer_durations_ms']}")

# PII summary CSV
csv_text = backend.get_text("pii-reports", f"{run_prefix}/pii_summary.csv")
reader   = csv.DictReader(io.StringIO(csv_text))
for row in reader:
    if row["detected"] == "True":
        print(f"  {row['column']:<20}  {row['entity_type']:<22}  "
              f"confidence={float(row['confidence']):.2f}")

# Raw detections JSON
detections_raw = json.loads(backend.get_text("pii-reports", f"{run_prefix}/pii_detections.json"))
for d in detections_raw:
    if d["detected"]:
        print(f"  {d['column']}: presidio={d['presidio_score']}  gliner={d['gliner_score']}")
```

### 12.6 List all tables in the contract store

```python
tables = store.list_tables()
for t in tables:
    active = store.get_active(t)
    print(f"  {t:<30}  v{active.get('version','?')}  "
          f"uuid={active.get('contract_uuid', '')[:8]}...")
```

---

## 13. Using the equation module standalone

The equation module is a pure Python function — no GE, no storage, no side effects.
Useful for unit testing detection logic or tuning thresholds in a notebook.

```python
from pii_detection import PIIDetection, Thresholds, decide_pii

# Simulate a raw detection coming out of detect_pii()
raw = PIIDetection(
    column        = "phone",
    detected      = False,      # always False from detector
    presidio_score= 0.85,
    gliner_score  = 0.75,
)

thresholds = Thresholds(
    presidio_min               = 0.60,
    gliner_min                 = 0.50,
    very_high_confidence_floor = 0.90,
)

# balanced: 2-of-3 engines vote yes → detected
result = decide_pii(raw, equation="balanced", thresholds=thresholds)
print(result.detected)     # True
print(result.confidence)   # 0.85  (max of engine scores)

# strict: all engines must pass → same result since both pass
result_strict = decide_pii(raw, equation="strict", thresholds=thresholds)
print(result_strict.detected)  # True

# lenient: any one engine above threshold
borderline = PIIDetection(column="notes", detected=False,
                           presidio_score=0.55, gliner_score=0.40)
result_lenient = decide_pii(borderline, equation="lenient", thresholds=thresholds)
print(result_lenient.detected)  # False (presidio=0.55 < 0.60 min; gliner=0.40 < 0.50 min)

# Tune the threshold
lower_t = Thresholds(presidio_min=0.50)
result_lower = decide_pii(borderline, equation="lenient", thresholds=lower_t)
print(result_lower.detected)   # True (presidio=0.55 >= 0.50)
```

---

## 14. Reference — full public API

All of the following can be imported directly from `pii_detection`:

```python
from pii_detection import (

    # ── Data contracts ────────────────────────────────────────────────────
    PIIDetection,            # detection result for one column
    PIIColumnProfile,        # GE triage signal for one column
    RunMetadata,             # run-level metadata

    # ── Classification helper ─────────────────────────────────────────────
    classify_sensitivity,    # entity_type → "pii_sensitive" | "pii_personal" | "internal"
    SENSITIVE_ENTITIES,      # frozenset of high-risk entity type names
    ARABIC_UNICODE_RE,       # regex string matching Arabic Unicode blocks
    PII_NAME_HINTS,          # tuple of column name tokens that hint at PII

    # ── Decisions ─────────────────────────────────────────────────────────
    Thresholds,              # per-engine confidence floors
    DEFAULT_EQUATION,        # "balanced"
    EQUATION_MODES,          # frozenset{"strict", "balanced", "lenient"}
    decide_pii,              # (PIIDetection, equation, Thresholds) → PIIDetection

    # ── Sampling ──────────────────────────────────────────────────────────
    SamplerConfig,           # strategy, partition_col, partition_date, ...
    PandasSampler,           # loads parquet, applies strategy
    PIISampler,              # Spark-based sampler (requires pyspark)

    # ── Pipeline config ───────────────────────────────────────────────────
    PipelineConfig,          # table, equation, thresholds, output_dir, ...
    GEPipelineConfig,        # table, output_dir, strategy, ...

    # ── Pipeline entry points ─────────────────────────────────────────────
    PIIPipeline,             # .run() → manifest dict  (Workflow B)
    GEPipeline,              # .run() → manifest dict  (Workflow A)

    # ── Storage ───────────────────────────────────────────────────────────
    S3Config,                # endpoint_url, credentials, bucket names
    StorageBackend,          # ABC: put_bytes/get_bytes/exists/list_keys/...
    LocalBackend,            # filesystem backend for development
    RunOutputWriter,         # writes per-run artefacts to pii-reports bucket
    get_backend,             # factory: S3Config → StorageBackend

    # ── Contract store ────────────────────────────────────────────────────
    ContractStoreService,    # upsert / get_active / get_history / list_tables
    UpsertResult,            # contract_uuid, version_before, version_after, ...
    TableHistoryEntry,       # run_uuid, workflow, timestamp, version, ...

    # ── Merge utilities ───────────────────────────────────────────────────
    merge_two_contracts,     # merge two ODCS dicts following the merge rules
    merge_odcs_contracts,    # multi-way merge helper
    IdentityConflictError,   # raised when locked identity fields mismatch

    # ── PII contract writer ───────────────────────────────────────────────
    PIIContractWriter,       # builds PII-only ODCS partial from detections
)
```

**Deeper imports for advanced use:**

```python
# GE profiler (requires pii-detection[ge])
from pii_detection.ge_classes.profiler  import DataProfiler
from pii_detection.ge_classes.gatekeeper import DataQualityGatekeeper

# Detection engines (requires pii-detection[ner])
from pii_detection.layers.ner_detector  import detect_pii
from pii_detection.layers.llm_refiner   import refine_with_llm

# Regex catalog
from pii_detection.catalogs.regex_catalog       import CATALOG, validate_luhn
from pii_detection.catalogs.recognizer_factory  import build_recognizers

# Report writers
from pii_detection.orchestrator.report_writer import (
    write_pii_run_outputs,
    write_ge_run_outputs,
)
```
