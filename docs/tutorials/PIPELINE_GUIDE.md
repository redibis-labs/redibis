# Redibis — End-to-End Pipeline Guide

**PII scan → contract creation → enrichment → masking / FPE before sharing or sending to an LLM.**

This guide is written for **data engineers** and **data scientists** who drive `redibis`
from either the **CLI** or a **Jupyter notebook / Python script**. Every command, flag,
option, strategy, and parameter shown here is taken directly from the `redibis` source,
so it is safe to copy-paste.

---

## Table of Contents

1. [What redibis does](#1-what-redibis-does)
2. [Installation & optional extras](#2-installation--optional-extras)
3. [Core concepts](#3-core-concepts)
4. [The full lifecycle at a glance](#4-the-full-lifecycle-at-a-glance)
5. [CLI reference (all commands)](#5-cli-reference-all-commands)
6. [Python / Jupyter API](#6-python--jupyter-api)
   - [6.1 Storage setup](#61-storage-setup)
   - [6.2 PII detection — every engine & option](#62-pii-detection--every-engine--option)
   - [6.3 Equations & thresholds](#63-equations--thresholds)
   - [6.4 Custom regex catalog overrides](#64-custom-regex-catalog-overrides)
   - [6.5 Full scan with `ScanService` (every config option)](#65-full-scan-with-scanservice-every-config-option)
   - [6.6 Contract store — upsert, validate, history, purge](#66-contract-store--upsert-validate-history-purge)
   - [6.7 Subcontracts & the run merger](#67-subcontracts--the-run-merger)
   - [6.8 LLM enrichment — every option](#68-llm-enrichment--every-option)
   - [6.9 Masking & FPE — every strategy & parameter](#69-masking--fpe--every-strategy--parameter)
7. [Complete end-to-end Jupyter notebook](#7-complete-end-to-end-jupyter-notebook)
8. [Reference tables](#8-reference-tables)

---

## 1. What redibis does

`redibis` is a data-contract pipeline for Cloudera CDP that produces and maintains
**ODCS v3** contracts. It is organized into one-way-dependent sub-packages:

| Sub-package | Responsibility |
|---|---|
| `redibis.store` | Generic ODCS contract storage — smart upsert, audit trail, the **single writer** to the contracts bucket. |
| `redibis.quality` | Great Expectations profiling + quality contracts. |
| `redibis.pii` | Presidio + GLiNER detection (+ optional LLM) → PII contracts. |
| `redibis.masking` | Value-level de-identification: mask / hash / encrypt / FPE / locale-aware fakers. |
| `redibis.enrich` | LLM enrichment of a full contract (business layer + PII refinement), validity-gated. |
| `redibis.services` | Orchestrators (`ScanService`, `pipeline`, `MaskingService`) used by CLI + webapp. |
| `redibis.cli` | The `redibis` command-line entry point. |
| `redibis.webapp` | FastAPI dashboard. |

**Key invariant — separation of evidence and verdict:** the detector produces *evidence*
(raw per-engine scores, `detected=False`); the *equation* (`decide_pii`) sets the final
`detected` verdict. This keeps detection engines and decision logic independently swappable.

---

## 2. Installation & optional extras

Core install is lightweight (storage + models + equation logic). Capabilities are layered
in via extras (from `pyproject.toml`):

```bash
pip install -e .                 # core (store, models, masking primitives)

pip install -e ".[ge]"           # Great Expectations profiling/quality
pip install -e ".[ner]"          # Presidio + GLiNER PII detection engines
pip install -e ".[enrich]"       # LiteLLM-backed enrichment (any LLM vendor)
pip install -e ".[mask]"         # faker + cryptography (AES-GCM) + pyarrow (parquet)
pip install -e ".[contracts]"    # datacontract-cli ODCS validation/linting
pip install -e ".[web]"          # FastAPI dashboard
pip install -e ".[spark]"        # PySpark sampling

pip install -e ".[all,dev]"      # everything + test tooling
```

> **Graceful degradation:** masking works offline without `faker` (embedded EN/AR pools)
> and without `cryptography` (falls back to an HMAC keystream cipher). PII detection
> returns an empty result set (instead of crashing) if `[ner]` engines are missing.

After install, the CLI is available as `redibis`:

```bash
redibis --help
```

---

## 3. Core concepts

**ODCS contract.** A YAML document with a `schema` (list of tables), each holding
`properties` (columns). Columns accumulate a `business` block, a `pii` block,
`quality` rules, `tags`, and a `classification`.

**Identity lock.** A contract's identity (`contract_uuid + database_name + table_name`)
is locked on first write. Conflicting identities raise `IdentityConflictError`.

**Merge semantics.** Tags merge as **set union**; `quality` lists are **replaced
wholesale**; every other field is **last-writer-wins**.

**`physicalName`.** The cross-workflow matching key — always set to `db.table`.

**Subcontracts vs. active contract.** A scan does **not** write to the active contract
directly. It writes a per-run **subcontract** into a type bucket (`pii-contracts` /
`quality-contracts`). You then **select-and-merge** a run into the active contract
(optionally automatically). This lets you review/discard bad runs.

**Equation modes** (how engine votes become a verdict): `strict`, `balanced`, `lenient`,
`independent` (default). See [§6.3](#63-equations--thresholds).

**Classification levels** (derived from `entity_type`): `pii_sensitive`, `pii_personal`,
or `internal`. See [§8](#8-reference-tables).

---

## 4. The full lifecycle at a glance

```
            ┌─────────────┐     ┌──────────────────┐     ┌───────────────┐
  data ───► │  1. SCAN    │ ──► │ 2. MERGE run into │ ──► │ 3. ENRICH     │
            │ PII+quality │     │  active contract  │     │ (LLM, gated)  │
            └─────────────┘     └──────────────────┘     └───────────────┘
                                                                │
                                                                ▼
            ┌──────────────────────────────────────────────────────────┐
            │ 4. MASK / FPE the data  → safe export (+ manifest, no keys)│
            │    auto-suggested from the PII verdicts in the contract    │
            └──────────────────────────────────────────────────────────┘
```

CLI happy path:

```bash
redibis scan data.csv --table telecom.customers --mode both --automerge both --use-local
redibis enrich telecom.customers --provider vllm --automerge --use-local
redibis mask auto data.csv --table telecom.customers --out data_safe.csv
```

---

## 5. CLI reference (all commands)

All store-backed commands accept these **common flags** (defined by `_common`):

| Flag | Default | Meaning |
|---|---|---|
| `--use-local` | off | Use a local on-disk backend instead of S3 (writes under `<output-dir>/_dev_storage`). |
| `--output-dir` | `./reports` | Where run artifacts (and local storage) go. |
| `--s3-endpoint` | – | S3-compatible endpoint URL. |
| `--s3-access-key` / `--s3-secret-key` | – | S3 credentials. |
| `--s3-contracts-bucket` | `active-contracts` | Active contract bucket. |
| `--s3-runs-bucket` | `pii-reports` | Run-artifact bucket. |
| `--s3-pii-runs-bucket` | `pii-contracts` | PII subcontract bucket. |
| `--s3-quality-runs-bucket` | `quality-contracts` | Quality subcontract bucket. |

### `redibis scan` — scan a file into run subcontracts (the v2 main flow)

```bash
redibis scan <file.csv|file.parquet> [options]
```

| Flag | Choices / default | Meaning |
|---|---|---|
| `--table` | from filename | `schema.table` identity. |
| `--mode` | `pii` \| `quality` \| `both` (=`both`) | Which workflows to run. |
| `--automerge` | `none` \| `pii` \| `quality` \| `both` (=`none`) | Auto-merge the run into the active contract. |
| `--no-merge` | flag | Force `--automerge none`. |
| `--equation` | `strict`\|`balanced`\|`lenient`\|`independent` (=`independent`) | Decision rule. |
| `--pii-engines` | `regex`\|`gliner`\|`ner`\|`llm`\|`both` (=`both`) | Which detection engines to run. |
| `--ner-model` | *(empty — BYOM)* | Path to a local NER model directory under `REDIBIS_MODELS_DIR`. |
| `--gliner-model` | *(deprecated alias of `--ner-model`)* | Same as `--ner-model`. |
| `--no-ge-docs` | flag | Skip generating GE Data Docs. |
| `--no-validate` | flag | Skip ODCS validation on merge. |

### `redibis runs` — review & merge run subcontracts

```bash
redibis runs list   <table> --kind pii|quality
redibis runs merge  <table> --kind pii|quality [--run <run_id>] [--no-validate]
redibis runs discard <table> --kind pii|quality --run <run_id>
```
`merge` without `--run` merges the latest non-discarded run.

### `redibis enrich` — LLM-enrich the full contract (validity-gated)

```bash
redibis enrich <table> [options]
redibis enrich --list-providers          # show configured LLM providers and exit
```

| Flag | Meaning |
|---|---|
| `--provider` | Provider name from `llm_providers.json` (default `vllm`). |
| `--providers-file` | Path to a provider registry JSON. |
| `--model` | Override the provider's default model (bare names are auto-prefixed). |
| `--endpoint` | Override `api_base` (local vLLM/Ollama). |
| `--api-key` | Inline API key. |
| `--context` | One or more context-doc file paths (design docs, data dictionaries, company info). |
| `--example-docs` | One or more example-doc file paths (uploaded few-shot examples). |
| `--examples` | Example contract **tables** to use as few-shot examples. |
| `--prompt` | System-prompt file (overrides the default steward prompt). |
| `--instructions` | Extra guidance (text or file path) appended to the system prompt. |
| `--automerge` | Validate **and** merge (fails closed if the candidate is invalid). |
| `--multistep` | YAML LangGraph stages (columns → privacy → table → review). |
| `--steps-file` | Workflow YAML (`config/examples/enrich-steps-default.yaml`). |
| `--pack` | Enrichment pack folder, `.zip`, or exported context-profile DIR. |

Context profile commands (same `enrich` entry point; `run` is optional):

```bash
redibis enrich run telecom.customers --provider vllm --multistep
redibis enrich export-context ./context-profile
redibis enrich add-context glossary.md --scope global --mode shared
redibis enrich list-context --scope global --json
redibis enrich remove-context shared/glossary.md --scope global
```

See [cli/enrich.md](../cli/enrich.md) for the full flag list and overlay examples.

### `redibis mask` / `redibis data` — de-identify (local files, no S3)

```bash
redibis data preview <file> [--rows N]

redibis mask plan  <file> [--from-pii] [--locale en|ar|mixed] [--out plan.yaml] [--table db.t]
redibis mask apply <file> --plan plan.yaml [--out data_safe.csv] [--format csv|parquet] [--seed S]
redibis mask auto  <file> [--locale ...] [--out ...] [--format ...] [--seed S]

redibis mask regex list [--json]
redibis mask regex test --library eg_mobile [-n 5]
redibis mask regex test --pattern '01[0125]\d{8}' [--deterministic] [--seed S]
```
- `plan` writes a reusable masking-plan YAML; `--from-pii` runs detection to pre-fill rules.
- `apply` applies a saved plan; `auto` detects + suggests + applies in one shot.
- Both `apply`/`auto` also write a `<out>.manifest.json` sidecar (**keys are never exported**).
- `regex list` / `regex test` — built-in pattern library for `fake` + `kind: regex` (uses `rstr` when installed for non-deterministic samples).

### Other store commands

```bash
redibis ge   --table db.t [--engine spark|pandas] [--strategy ...]   # quality-only (pre-sampled parquet)
redibis pii  --table db.t [--equation ...] [--presidio-min ...] [--enable-llm]  # PII-only (pre-sampled)
redibis import-business --file biz.yaml --table db.t                 # import business glossary
redibis merge   --table db.t                                         # print merged ODCS
redibis show    --table db.t                                         # print active contract YAML
redibis history --table db.t                                         # audit trail
redibis list                                                         # list tables with active contracts
redibis rules list   <table>                                         # list quality rules
redibis rules export <table> --target ge|sodacl|dbt                  # regenerate rule artifacts
redibis quality-monitor run <table> --sample file.csv [--json]               # validate-only (no contract write)
redibis quality-monitor batch --all-contracts [--json]                       # batch monitor all active contracts
redibis quality-monitor export <table> -o ./packages                         # deployment package (rules + DAGs)
redibis quality-monitor airflow generate --all-contracts -o ./dags           # Airflow DAG codegen
redibis contract purge <table> [--keep-runs]                         # fresh start
```

> **Discovery vs monitoring:** `redibis scan` proposes rules and may merge subcontracts;
> `redibis quality-monitor` re-validates the **approved active contract** only. See
> [`docs/cli/monitor.md`](../cli/monitor.md).

> Note: `redibis scan` is the primary entry point. `redibis ge` / `redibis pii` are the
> older workflows that operate on a **pre-sampled parquet** in `--output-dir`.

---

## 6. Python / Jupyter API

### 6.1 Storage setup

```python
from redibis.store.storage_backend import LocalBackend, S3Backend, S3Config, get_backend
from redibis.store.contract_store import ContractStore
from redibis.store.subcontract_store import SubcontractStore

# Local (development / notebooks)
backend = LocalBackend("./dev_storage")

# OR S3 / MinIO
# backend = get_backend(S3Config(
#     endpoint_url="https://s3.example.com",
#     aws_access_key_id="...", aws_secret_access_key="...",
# ))

store = ContractStore(backend, bucket="active-contracts")          # single writer
sub_store = SubcontractStore(backend,
                             pii_bucket="pii-contracts",
                             quality_bucket="quality-contracts")
```

### 6.2 PII detection — every engine & option

The lowest-level entry point is `detect_pii` (evidence only). The shared `pipeline`
module wraps detection **plus** the equation in one call (`run_pii_detection`).

```python
from redibis.services.pipeline import run_pii_detection
from redibis.pii.thresholds import Thresholds

detections = run_pii_detection(
    df,                                  # a pandas (or Spark) DataFrame
    columns=None,                        # None = all columns; or ["phone", "email", ...]
    engines="both",                      # "regex" | "gliner" | "llm" | "both"
    gliner_always_run=False,             # force NER even when regex already scored >= 0.80
    regex_overrides=None,                # custom catalog (see §6.4)
    gliner_config=None,                  # GlinerConfig(model_id=..., device=..., batch_size=...)
    equation_mode="independent",         # strict | balanced | lenient | independent
    thresholds=Thresholds(),             # per-engine floors (see §6.3)
    progress_callback=print,             # optional log sink
)

for d in detections:
    print(d.column, d.detected, d.entity_type, d.confidence,
          "engines:", d.contributing_engines())
```

Each result is a `PIIDetection` dataclass. Notable fields:

- `detected`, `confidence`, `entity_type`, `equation_used`, `decision_path`
- raw scores: `presidio_score`, `gliner_score`, `llm_score` (+ `*_pattern`/`*_label`/`*_match_rate`)
- context: `arabic_aware`, `arabic_fraction`, `triage_score`
- `classification` (property) → `pii_sensitive` / `pii_personal` / `internal`
- GE hint: `regex_pattern`, `suggested_mostly`

Calling the detector directly (evidence only — `detected` stays `False`):

```python
from redibis.pii.detector import detect_pii
raw = detect_pii(df, columns=None, engines="both", gliner_always_run=False)
```

### 6.3 Equations & thresholds

`Thresholds` controls the confidence **bar** per engine; the **equation** controls the
voting logic. They are independent.

```python
from redibis.pii.thresholds import Thresholds
from redibis.pii.equations import decide_pii, decision_rule_text

t = Thresholds(
    presidio_min=0.80,                 # regex floor
    gliner_min=0.70,                   # NER floor
    llm_min=0.82,                      # LLM floor
    ge_triage_min=0.0,                 # min triage score to send a column to the detector
    very_high_confidence_floor=0.90,   # single-engine shortcut for "balanced"
)

verdict = decide_pii(raw_detection, "balanced", t)
print(decision_rule_text("balanced", t))
```

| Mode | Rule |
|---|---|
| `strict` | **All** available engines must pass their floor. |
| `balanced` | **2-of-3** engines pass, **OR** any single engine ≥ `very_high_confidence_floor`. |
| `lenient` | **Any** single engine ≥ its floor. |
| `independent` (default) | Any single engine ≥ its floor, evaluated independently. |

You can also build the formal per-column report:

```python
from redibis.pii.equations import build_column_report
report = build_column_report(verdict, thresholds=t)   # PIIColumnReport
print(report.to_dict())
```

### 6.4 Custom regex catalog overrides

The built-in catalog ships dozens of patterns (Egyptian phone formats, national ID, tax
ID, passport, IBAN/SWIFT, credit card, IMSI/ICCID/IMEI, IP/MAC, OTP, JWT, API keys,
crypto wallets, GDPR special categories, and more). You can **add** to it or **replace**
it entirely per run.

```python
from redibis.pii.regex_overrides import RegexOverrides
from redibis.services.pipeline import run_pii_detection

overrides = RegexOverrides(
    add={
        "my_employee_id": {
            "pattern": r"^EMP\d{6}$",
            "entity_type": "EMPLOYEE_ID",
            "recognizer_group": "structured",     # "structured" | "free_text"
            "presidio_score": 0.95,
            "context_hints": ("employee", "emp", "staff"),
        },
    },
    replace_all=False,   # True = ignore the default catalog, use only `add`
)

# Persist / reload
overrides.to_yaml("my_regex.yaml")
overrides = RegexOverrides.from_yaml("my_regex.yaml")

detections = run_pii_detection(df, regex_overrides=overrides, engines="regex")
```

### 6.5 Full scan with `ScanService` (every config option)

`ScanService` runs the entire pipeline (sample → profile → quality gatekeeper →
PII detect → equation → contract writers → subcontract write → optional merge) and
returns a `ScanResult` with artifact links.

```python
from pathlib import Path
from redibis.services.scan_service import ScanService, ScanConfig, GlinerConfig, LLMConfig
from redibis.quality.sampling import SamplingConfig
from redibis.pii.thresholds import Thresholds
from redibis.pii.regex_overrides import RegexOverrides

service = ScanService(backend=backend, store=store,
                      runs_bucket="pii-reports", sub_store=sub_store)

config = ScanConfig(
    table="telecom.customers",
    equation_mode="balanced",
    run_pii=True,
    run_quality=True,
    generate_ge_docs=True,
    validate_contracts=True,
    automerge="both",                       # "none" | "pii" | "quality" | "both"
    output_dir=Path("./reports"),

    # Sampling (optional — omit to scan the whole DataFrame)
    sampling_config=SamplingConfig(),
    triage_threshold=0.0,

    # PII granular control
    pii_columns=None,                       # restrict to specific columns
    pii_engines="both",                     # "regex" | "gliner" | "llm" | "both"
    pii_regex_confidence=None,              # → Thresholds.presidio_min
    pii_gliner_confidence=None,             # → Thresholds.gliner_min
    pii_llm_confidence=None,                # → Thresholds.llm_min
    pii_gliner_always_run=False,
    pii_regex_overrides=None,               # RegexOverrides(...)
    thresholds=None,                        # full Thresholds(...) overrides the *_confidence floors
    gliner_config=GlinerConfig(model_id=""),  # legacy; prefer pii.ner.model_path
    ner_config=NERConfig(model_path="/models/my-ner"),  # BYOM local weights
    llm_config=None,                        # LLMConfig(...) for the LLM engine
)

result = service.scan_csv("data.csv", config)         # or service.scan_dataframe(df, config)
# Other entry points: service.scan_bytes(file_bytes, "data.csv", config)

print(result.status, result.total_rows, result.total_columns)
print("PII detected:", result.pii_columns_detected, "/", result.pii_columns_scanned)
print("Quality:", result.quality_passed, "/", result.quality_expectations)
print("Contract versions:", result.pii_contract_version, result.quality_contract_version)
print("Artifacts:", result.artifacts)   # triage_report, pii_contract, ge_report, ...
```

If `automerge` doesn't cover a kind, the run is written to its bucket for later review
(see [§6.7](#67-subcontracts--the-run-merger)).

### 6.6 Contract store — upsert, validate, history, purge

```python
# Read
active = store.get_active("telecom.customers")        # dict | None
tables = store.list_tables()
history = store.get_history("telecom.customers", limit=50)

# Write a partial (merged via smart upsert — the ONLY way to write)
result = store.upsert(partial_contract, table="telecom.customers",
                      workflow="pii", run_id="r1", validate=False)
print(result.version_after, result.is_new, result.contract_uuid)

# Validate against ODCS (Pydantic + datacontract-cli lint)
report = store.validate(active, strict=False)
print(report["valid"], report["errors"], report["warnings"])

# Fresh start (keep run buckets so good runs can be re-merged)
store.purge("telecom.customers")
```

### 6.7 Subcontracts & the run merger

```python
from redibis.store.run_merger import RunMerger
merger = RunMerger(store, sub_store)

# List runs in a type bucket
for r in sub_store.list_run_summaries("pii", "telecom.customers"):
    print(r["run_id"], r["status"], r.get("summary_stats"))

# Merge a specific run (or the latest non-discarded one)
res = merger.merge_run("pii", "telecom.customers", run_id="2026-05-31_00-00-00", validate=True)
res = merger.merge_latest("quality", "telecom.customers", validate=True)
print(res.run_id, "→ v", res.upsert.version_after)

# Discard a bad run
merger.discard_run("pii", "telecom.customers", run_id="2026-05-31_00-00-00")
```

### 6.8 LLM enrichment — every option

Enrichment loads the **full active contract**, optional **context docs** and **example
docs/contracts**, sends it to any LiteLLM-backed provider, applies the returned delta to
produce a **candidate**, validates it, and holds it until you merge. Merge is
**validity-gated** (fails closed).

```python
from redibis.enrich.service import EnrichmentService
from redibis.enrich.providers import get_provider, list_providers

svc = EnrichmentService(store)

# Inspect configured providers (vllm, ollama, openrouter, claude, gemini, openai by default)
for p in list_providers():
    print(p["name"], p["model"], "needs_key" if p["needs_key"] else "")

# Attach grounding material (optional)
svc.add_context_doc("telecom.customers", "dictionary.md",
                    b"phone = primary mobile in E.164; national_id = 14-digit Egyptian NID")
svc.add_example_doc("telecom.customers", "good_contract.yaml", open("example.yaml","rb").read())

# Build a provider (vendor encoded entirely in the registry; one identical code path)
provider = get_provider(
    "vllm",
    model="Qwen/Qwen2.5-7B-Instruct",        # optional override (auto-prefixed)
    endpoint_url="http://localhost:8000/v1", # optional api_base override
    api_key=None,                            # else taken from the provider's api_key_env
)

result = svc.enrich(
    "telecom.customers",
    provider,
    system_prompt=None,                      # None → EnrichmentService.default_system_prompt()
    extra_instructions="Mark salary as pii_sensitive; keep definitions under 200 chars.",
    example_contracts=["telecom.gold_customers"],   # few-shot from existing tables
    enriched_by="data_scientist",
)

print("valid:", result.valid, "errors:", result.errors)
print("candidate columns:", list(result.candidate.get("schema", [{}])[0].get("properties", [])))

# Review then merge (raises ValueError if invalid — fails closed)
if result.valid:
    merged = svc.merge_candidate("telecom.customers")
    print("merged → v", merged["version_after"])
```

The model returns (and `apply_enrichment` applies) a delta of this shape:

```json
{
  "table": {
    "description": "Customer master: one row per subscriber at activation.",
    "purpose": "Subscriber registry"
  },
  "table_tags": ["telecom", "gdpr"],
  "columns": {
    "phone": {
      "business": {"definition": "...", "synonyms": ["msisdn"],
                   "example_values": ["+20..."], "tags": ["contact"]},
      "pii": {"classification": "pii_personal", "entity_type": "PHONE_NUMBER"},
      "tags": ["regulated"]
    }
  }
}
```
Business blocks are replaced wholesale; PII classification/entity are refined; tags union
onto columns and the table.

### 6.9 Masking & FPE — every strategy & parameter

The masking engine produces a safe-to-share copy. It guarantees utility: deterministic
transforms preserve **joins**; FPE preserves **format/length**; faked emails are derived
from faked names; Arabic vs. English is auto-detected with `locale="mixed"`.

#### Build a plan (auto-suggested from PII, fully overridable)

```python
from redibis.masking import (auto_suggest_plan, suggest_rule,
                             MaskingPlan, ColumnMaskRule,
                             MaskingEngine, RunKeys, risk_report)

detections = [
    {"column": "name",        "detected": True,  "entity_type": "PERSON"},
    {"column": "email",       "detected": True,  "entity_type": "EMAIL"},
    {"column": "phone",       "detected": True,  "entity_type": "PHONE_NUMBER"},
    {"column": "national_id", "detected": True,  "entity_type": "NATIONAL_ID"},
    {"column": "city",        "detected": False, "entity_type": None},
]

plan = auto_suggest_plan("telecom.customers", list(df.columns), detections,
                         default_locale="mixed")     # "en" | "ar" | "mixed"

# Override any rule explicitly
plan.rule_for("national_id").strategy = "fpe"
plan.rule_for("national_id").params = {"alphabet": "digits", "key_ref": "k1"}
plan.rule_for("city").include_in_scan = False        # drop from active column list

# Save / reload the plan (reusable across tables & sessions)
yaml_text = plan.to_yaml()
plan = MaskingPlan.from_yaml(yaml_text)
```

#### Apply with per-run keys

```python
keys = RunKeys.mint(seed="secure-session-seed")      # omit seed → random; same seed → reproducible
engine = MaskingEngine(plan, keys)
masked_df = engine.transform_dataframe(df)

# Side-by-side preview for a UI
preview = engine.compare_sample(df, rows=10)

# Risk panel — weak/leaky choices
for f in risk_report(df, plan):
    print(f"[{f['severity']}] {f['column']}: {f['issue']} — {f['detail']}")
```

> **Key handling (invariant #11):** keys/seed are minted **per run**, stored only under
> the session's `_meta/env`, and **never** written into the export or manifest. A new run
> regenerates keys, so exports are not cross-linkable unless you reuse the seed.

#### Strategies and their parameters

| Strategy | Reversible? | Key params (`rule.params`) |
|---|---|---|
| `passthrough` | n/a | — (leaves the column unchanged) |
| `redact` | no | `replacement` (default `None`) |
| `mask` | no | `keep_first` (0), `keep_last` (4), `mask_char` (`*`), `preserve_length` (True) |
| `hash` | no | `algo` (`sha256`), `hmac_key_ref` (e.g. `k1`), `truncate` (int), `prefix` |
| `encrypt` | yes (key-holder) | `key_ref` (`k1`) — AES-256-GCM if `cryptography` present, else HMAC keystream |
| `fpe` | yes (key-holder) | `alphabet` (`digits`\|`alnum`\|custom string), `tweak`, `key_ref` |
| `fake` | no | `kind` (see below), `locale`, `deterministic`, `preserve {...}`, `jitter_days`, `key_ref` |

**Position slicing (any row above except `passthrough` / `redact`):** optional
`start_index` (0-based inclusive) and `end_index` (0-based exclusive; omit for end of string).
Example: FPE only on digits after a country prefix — `start_index: 2` on `01012345678`.

**`fake` kinds:** `name`, `phone`, `email`, `address`, `company`, `national_id`,
`credit_card`, `iban`, `date`, `uuid`, `free_text`, `regex`.

**`fake` + `regex`:** `regex_library` (e.g. `eg_mobile`) or inline `regex_pattern`.
Library: packaged `redibis/masking/regex_patterns.json`, overridable via
`REDIBIS_REGEX_PATTERNS` or `./regex_patterns.json`. CLI: `redibis mask regex list|test`.

`preserve` sub-options by kind: `email → {domain}`, `phone → {format, country_code, region}`,
`name → {gender}`. `date → {jitter_days}`.

#### Standalone transform primitives (`redibis.masking.transforms`)

```python
from redibis.masking import transforms as T

# Format-Preserving Encryption (reversible, keeps separators/length)
key = b"0" * 32
enc = T.fpe_transform("123-456-789", key, alphabet="digits", tweak="ssn")
dec = T.fpe_transform(enc, key, alphabet="digits", tweak="ssn", decrypt=True)   # "123-456-789"

# Authenticated reversible encryption
tok = T.encrypt_value("secret@example.com", b"k"*32)
T.decrypt_value(tok, b"k"*32)

# Non-reversible
T.hash_value("hello", hmac_key=b"salt", truncate=12)
T.mask_value("4111111111111234", keep_last=4)          # "************1234"

# Locale-aware fakers via the deterministic PRNG
rng = T.KeyedRandom(b"run-key", "name")
T.fake_name(rng, "ar")                                  # "أحمد الحسيني"
T.fake_phone(rng, "+20 100 123 4567", preserve_country_code=True)

# Position slice: mask only the tail of a value
T.apply_position_slice("01012345678", lambda seg: T.mask_value(seg, keep_last=0),
                       start_index=2)

# Regex-shaped fake (deterministic uses keyed PRNG; non-deterministic uses rstr if installed)
from redibis.masking.regex_library import resolve_pattern
pat = resolve_pattern("eg_mobile")
rng = T.KeyedRandom(b"run-key", "mobile|010")
T.fake_from_regex(rng, pat, deterministic=True)

# What's available in this environment?
T.capabilities()   # {'faker', 'aes_gcm', 'rstr', 'position_slicing', 'fake_kinds', ...}
```

#### Session-scoped masking (`MaskingService`)

When you already have a scan **session** (web/CLI flows), `MaskingService` wires the plan,
preview, key minting (under `_meta/env`), and export+manifest together:

```python
from redibis.services.masking_service import MaskingService
ms = MaskingService(session)
ms.preview_mask(row=0)                 # single-record raw vs masked + risk
out = ms.apply_export(fmt="csv")       # writes masked file + manifest (keys kept back)
print(out["export_path"], out["manifest_path"])
```

---

## 7. Complete end-to-end Jupyter notebook

A self-contained script: synthesize data → scan (PII + quality) → auto-merge contract →
enrich → drive the masking plan from the contract's PII verdicts → FPE/fake export.

```python
import pandas as pd
from pathlib import Path

from redibis.store.storage_backend import LocalBackend
from redibis.store.contract_store import ContractStore
from redibis.store.subcontract_store import SubcontractStore
from redibis.services.scan_service import ScanService, ScanConfig
from redibis.enrich.service import EnrichmentService
from redibis.enrich.providers import get_provider
from redibis.masking import auto_suggest_plan, MaskingEngine, RunKeys, risk_report

# 1) Synthetic, EN + AR data
df = pd.DataFrame({
    "name":        ["Ahmed Hassan", "Sara Smith", "محمد علي"],
    "email":       ["ahmed.hassan@company.com", "sara.smith@gmail.com", "m.ali@yahoo.com"],
    "phone":       ["+20 100 123 4567", "+1 555 019 2834", "+20 122 987 6543"],
    "national_id": ["29901011234567", "11122233344455", "29508151234568"],
    "salary":      [50000, 75000, 62000],
})
df.to_csv("customers.csv", index=False)

# 2) Stores
backend   = LocalBackend("./redibis_storage")
store     = ContractStore(backend, bucket="active-contracts")
sub_store = SubcontractStore(backend, pii_bucket="pii-contracts",
                             quality_bucket="quality-contracts")

# 3) Scan (PII + quality) and auto-merge both into the active contract
scan = ScanService(backend, store, runs_bucket="pii-reports", sub_store=sub_store)
scan_result = scan.scan_csv("customers.csv", ScanConfig(
    table="telecom.customers",
    equation_mode="balanced",
    run_pii=True, run_quality=True,
    automerge="both",
    output_dir=Path("./reports"),
))
print("scan:", scan_result.status,
      "| pii detected:", scan_result.pii_columns_detected,
      "| contract v:", scan_result.pii_contract_version)

# 4) Enrich (skip gracefully if no LLM endpoint is reachable)
try:
    enrich = EnrichmentService(store)
    provider = get_provider("vllm")                  # uses ./llm_providers.json or packaged defaults
    er = enrich.enrich("telecom.customers", provider,
                       extra_instructions="Mark salary as pii_sensitive.",
                       enriched_by="jupyter")
    if er.valid:
        enrich.merge_candidate("telecom.customers")
        print("enriched → merged")
    else:
        print("enrichment invalid:", er.errors)
except Exception as e:
    print("enrichment skipped:", e)

# 5) Drive the masking plan from the contract's PII verdicts
active = store.get_active("telecom.customers")
props  = active["schema"][0]["properties"]
detections = [{"column": p["name"],
               "detected": bool(p.get("pii", {}).get("detected")),
               "entity_type": p.get("pii", {}).get("entity_type")}
              for p in props]

plan = auto_suggest_plan("telecom.customers", list(df.columns),
                         detections, default_locale="mixed")

# 6) Apply: national_id → FPE (reversible, format-preserving); name/email/phone → locale fakers
keys   = RunKeys.mint(seed="secure-session-seed")
safe   = MaskingEngine(plan, keys).transform_dataframe(df)

print("\nRisk panel:")
for f in risk_report(df, plan):
    print(f"  [{f['severity']}] {f['column']}: {f['issue']}")

print("\nORIGINAL:\n", df)
print("\nDE-IDENTIFIED (safe to send to an LLM):\n", safe)
safe.to_csv("customers_safe.csv", index=False)
```

---

## 8. Reference tables

### Equation modes

| Mode | Verdict rule |
|---|---|
| `strict` | All available engines pass their floor. |
| `balanced` | 2-of-3 pass, OR any engine ≥ `very_high_confidence_floor` (0.90). |
| `lenient` | Any single engine ≥ its floor. |
| `independent` *(default)* | Any single engine ≥ its floor (independent evaluation). |

### Default thresholds

| Field | Default |
|---|---|
| `presidio_min` (regex) | 0.80 |
| `gliner_min` (NER) | 0.70 |
| `llm_min` | 0.82 |
| `ge_triage_min` | 0.0 |
| `very_high_confidence_floor` | 0.90 |

### Classification (derived from `entity_type`)

| Level | Example entity types |
|---|---|
| `pii_sensitive` | `EG_NATIONAL_ID`, `NATIONAL_ID`, `PASSPORT`, `CREDIT_CARD`, `CRYPTO_WALLET`, `API_KEY`, `PASSWORD_HASH`, `JWT_TOKEN`, `GDPR_SPECIAL_CATEGORY`, `EG_TAX_ID`, `IBAN_CODE` |
| `pii_personal` | `PHONE_NUMBER`, `EMAIL_ADDRESS`, `PERSON`, `LOCATION`, `DATE_TIME`, `IMSI`, `IMEI`, `IP_ADDRESS`, … |
| `internal` | unknown / `None` entity type |

### Masking strategies → reversibility

| Strategy | Reversible | Use for |
|---|---|---|
| `passthrough` | — | non-sensitive columns |
| `redact` | no | drop the value entirely |
| `mask` | no | keep a suffix (last-4) for support workflows |
| `hash` | no | stable pseudonyms (use an HMAC salt!) |
| `encrypt` | yes (key-holder) | reversible text columns |
| `fpe` | yes (key-holder) | IDs / cards / SSNs that must keep format & joins |
| `fake` | no | realistic, locale-aware synthetic values |

### `fake` kinds

`name`, `phone`, `email`, `address`, `company`, `national_id`, `credit_card`, `iban`,
`date`, `uuid`, `free_text`. Locales: `en`, `ar`, `mixed` (auto-detect Arabic).

### Default LLM providers (`llm_providers.json`)

`vllm` (local), `ollama` (local), `openrouter`, `claude`, `gemini`, `openai` — all routed
through LiteLLM. Add or edit providers declaratively; no code changes needed.
