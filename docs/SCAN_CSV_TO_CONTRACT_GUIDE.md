# Scanning a CSV → reports + contract.yaml (+ masked CSV) — full guide

Give redibis a CSV file and get back: a **quality + PII scan** (with options), the **report
artifacts** (PII report, GE quality report, profile/triage), the **ODCS `contract.yaml`**, and a
**masked copy of the CSV** (with the default suggested rules, or your overrides). Plus a **CLI for
batch** scanning a folder of CSVs into contracts.

> Grounded in the current source (CLI flags, `ScanService`, masking API). The dev shell was down
> while writing — snippets are accurate-to-the-API; smoke them in your venv.

---

## 1. Quick start (CLI — one CSV → everything)

```bash
# full scan (profile + quality + PII) → reports under ./reports, run subcontracts written
redibis scan data/customers.csv telecom.customers --mode all --automerge both --output-dir ./reports

# get the resulting ODCS contract as YAML
redibis show telecom.customers > telecom.customers.contract.yaml
```
- `redibis scan FILE [TABLE]` — TABLE defaults from the filename if omitted.
- `--mode all` runs profile + quality + PII; `--automerge both` merges the PII + quality run into the
  active contract immediately (else the run is staged as a subcontract you merge later).
- Artifacts (HTML reports + GE data docs) land under `--output-dir/<run_id>/`.
- `redibis show <table>` prints the active contract YAML (redirect to a file to save it).

Profile-only or quality-only as their own subcommands:
```bash
redibis profile data/customers.csv telecom.customers            # profile, no GE/PII
redibis quality data/customers.csv telecom.customers            # profile + GE validation, no PII
```

---

## 2. CLI reference (scan / profile / quality)

`redibis scan FILE [TABLE]` flags (shared by `profile` / `quality`):

| Flag | Meaning |
|---|---|
| `--mode all\|profile\|quality\|pii` | what to run (default `all` for `scan`) |
| `--automerge none\|pii\|quality\|both` | merge the run into the active contract (default `none` = stage only) |
| `--equation strict\|balanced\|lenient\|independent` | PII decision strictness |
| `--pii-engines regex\|gliner\|ner\|llm\|both` | which PII detectors to run |
| `--ner-model` / `--gliner-model` | path/name of a local NER model (air-gapped/BYOM) |
| `--profiler-engine great_expectations\|open_metadata` | profiling engine (GE rules vs lean metrics) |
| `--no-ge-docs` | skip GE data-docs HTML (faster) |
| `--no-validate` | skip contract validation |
| `--output-dir DIR` | where reports/run artifacts go (default `./reports`) |
| `--debug` | enable DEBUG logging (global flag, before subcommand) |
| `--log-format rich\|json\|plain` | log format (global; default rich on TTY, else json) |
| `--session-id` / `--scan-output-dir` | reuse a session / set the session root |
| `--config FILE` | a `redibis.yaml` (RedibisConfig) to drive everything |
| `--use-s3` + `--s3-endpoint` + `--s3-*-bucket` | write to S3/MinIO instead of local FS |

Per-scan log (when `observability.persist_run_log` is true, the default):

```
<output-dir>/<run_id>/<table>.<run_id>.log
```

See `docs/OBSERVABILITY.md` for env vars, decision channel, and optional OTel.

Contract lifecycle after a scan:
```bash
redibis runs list telecom.customers              # staged run subcontracts
redibis runs merge telecom.customers <run_id>    # merge a staged run into active
redibis show telecom.customers                   # active contract YAML
redibis rules list telecom.customers             # quality rules; `rules export` → GE/Soda/dbt
```

---

## 3. Python — `ScanService.scan_csv` (CSV → S3/FS → contract + reports)

```python
from redibis.services.scan_service import ScanService, ScanConfig
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend            # or S3Backend

backend = LocalBackend("./_local_storage")
store   = ContractStore(backend=backend, bucket="active-contracts")
svc     = ScanService(backend=backend, store=store)

result = svc.scan_csv(
    "data/customers.csv",
    config=ScanConfig(
        table="telecom.customers",
        run_profile=True, run_quality=True, run_pii=True,   # the three scan types
        profiler_engine="great_expectations",               # or "open_metadata"
        equation_mode="balanced",                           # PII strictness
        pii_engines="both",                                 # regex + NER
        generate_ge_docs=True,                              # GE quality report HTML
        validate_contracts=True,
        automerge="both",                                   # merge pii+quality into active
        output_dir="./reports",
    ),
)
print(result.status, result.run_id)
print(result.artifacts)        # {pii_detection_report, interactive_review, ge_report, ...} → paths/keys
print(result.quality_passed, "/", result.quality_expectations, "expectations")
print(result.pii_columns_detected, "PII columns")
active = store.get_active("telecom.customers")     # the contract dict → dump to YAML if you like
```

`ScanService.scan_dataframe(df, config)` is the same for an in-memory frame; `scan_bytes(file_bytes,
filename, config)` for uploads. The **facades** are the lighter library entry when you only want
results in memory:
```python
from redibis.scan import PIIScan, QualityScan, ProfileScan, ScanConfig
pii = PIIScan(ScanConfig(table="telecom.customers")).run(df, run_dir="./reports")   # PIIScanResult
```

---

## 4. The artifacts you get

Under `output_dir/<run_id>/` (or S3 keys / session artifacts):

| Artifact | What it is | Produced by |
|---|---|---|
| `pii_detection_report.html` | per-column PII detections + entities + confidence | `ReportBundle` / `export_pii_detection_report` |
| `interactive_review.html` | GE-backed **quality report** (expectations pass/fail) | quality gatekeeper |
| `ge_report/index.html` | full GE **data docs** | gatekeeper `copy_data_docs_to` |
| `triage_report.html` | profile/triage (which columns went to the detector) | profiler |
| `pii_contract.yaml` / `quality_contract.yaml` | the per-run subcontract partials | scan writers |
| **active contract** (`redibis show <table>`) | the merged ODCS v3 `contract.yaml` | `ContractStore` (`active/`) |

The contract YAML is the durable deliverable; the HTML reports are the run evidence. In the web app
these surface at `/api/sessions/{sid}/artifacts/<key>` (dashboard session required —
[`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md)).

---

## 5. Batch — a folder of CSVs → contracts + reports (CLI)

A small batch driver (there's also `scripts/batch_scan.sh` in the repo):

```bash
#!/usr/bin/env bash
# scan every CSV in a folder → contracts + per-file reports + a roll-up
set -euo pipefail
IN_DIR="${1:-./data}"; OUT="${2:-./reports/batch_$(date +%F)}"; mkdir -p "$OUT"
SUMMARY="$OUT/summary.csv"; echo "file,table,status,pii_cols,quality_passed" > "$SUMMARY"

for csv in "$IN_DIR"/*.csv; do
  table="telecom.$(basename "$csv" .csv)"          # derive schema.table from filename
  echo ">> scanning $csv → $table"
  redibis scan "$csv" "$table" --mode all --automerge both \
      --equation balanced --pii-engines both --output-dir "$OUT" || { echo "FAILED $csv"; continue; }
  redibis show "$table" > "$OUT/${table}.contract.yaml"
  echo "$csv,$table,ok,,," >> "$SUMMARY"   # (parse run stats from the report for richer columns)
done
echo "Contracts + reports in $OUT ; roll-up: $SUMMARY"
```
Each file produces: its report artifacts under `$OUT/<run_id>/`, a `<table>.contract.yaml`, and a row
in `summary.csv`. For **continuous validation** of merged rules (no re-discovery), use
`redibis quality-monitor batch` or Airflow DAGs from `redibis quality-monitor airflow generate` — see
[`docs/cli/monitor.md`](cli/monitor.md) and `docs/QUALITY_SCAN_ALL_WAYS.md` §6.

Python batch (more control over reports/roll-up):
```python
import glob, pathlib
for csv in glob.glob("data/*.csv"):
    table = f"telecom.{pathlib.Path(csv).stem}"
    r = svc.scan_csv(csv, config=ScanConfig(table=table, automerge="both", output_dir="./reports"))
    print(table, r.status, r.artifacts.get("pii_detection_report"))
```

---

## 6. Masked copy of the CSV (default suggestion + overrides)

redibis can emit a **de-identified CSV** built from the PII scan. The suggested rules come from the
detected entities; you can take them as-is (**default**) or **override** any column. Keys are minted
per run and **never** written into the export (invariant #11).

### 6.1 Default suggestion (auto-from-PII)
```python
from redibis.masking.plan import auto_suggest_plan
from redibis.masking.engine import MaskingEngine, RunKeys, risk_report
import pandas as pd

df = pd.read_csv("data/customers.csv")

# detections from a PII scan (list of {column, entity_type, detected}); from result.* or PIIScan
detections = [{"column": d.column, "entity_type": d.entity_type, "detected": d.detected}
              for d in pii_detections]

plan = auto_suggest_plan("telecom.customers", list(df.columns), detections, default_locale="en")
# -> e.g. email→fake(email, keep domain), national_id→FPE(digits), name→fake(name), phone→fake(phone)

keys   = RunKeys.mint()                      # per-run secret material (seed + master key)
masked = MaskingEngine(plan, keys).transform_dataframe(df)
masked.to_csv("data/customers.masked.csv", index=False)

# safety: flag weak choices (e.g. passthrough on a PII column)
for warn in risk_report(df, plan):
    print("RISK:", warn)
```
Default strategies (from `suggest_rule`): email→`fake` (keep domain), phone→`fake` (keep
format/country), name/address/company/date→`fake`, national_id/SSN/credit_card/IBAN→`fpe`
(format-preserving, joins survive), other-PII→`hash` (stable pseudonym), non-PII→`passthrough`.

### 6.2 Overridden suggestion (edit any column's rule)
```python
# start from the suggested plan, then override specific columns:
plan.rule_for("national_id").strategy = "hash"          # FPE → hash instead
plan.rule_for("national_id").params   = {"algo": "sha256", "hmac_key_ref": "k1", "truncate": 16}

plan.rule_for("email").params["preserve"] = {"domain": False}   # don't keep the domain
plan.rule_for("balance").strategy = "passthrough"               # keep a non-PII numeric as-is
plan.rule_for("dob").strategy = "fake"; plan.rule_for("dob").params = {"kind": "date", "jitter_days": 7}

masked_override = MaskingEngine(plan, keys).transform_dataframe(df)
masked_override.to_csv("data/customers.masked.override.csv", index=False)
```
Strategies you can set per `ColumnMaskRule`: `passthrough` · `mask` · `hash` · `encrypt` (AES-GCM) ·
`fpe` (FF3-1 format-preserving) · `fake` (locale-aware: en/ar; kinds: name/email/phone/address/
company/date) · `regex` (pattern-based). `deterministic=True` keeps joins stable across the dataset.

### 6.3 Session/web + CLI masking
- Web/session: `masking_service` stores the plan per session, previews original-vs-masked
  (`MaskingEngine.compare_sample`), and applies → a **safe export + manifest** (the manifest records
  strategies + key *references*, never key bytes/seed).
- CLI: `redibis mask --help` (regex library: `redibis mask regex list|test`). A new run regenerates
  keys/seed, so two exports are not cross-linkable unless you reuse the seed (`RunKeys.mint(seed=...)`).

---

## 6b. PII columns get NO quality rules (automated scan only)

GE quality rules embed **real column values** — `min/max`, `validValues`, quantiles,
`median/mean/stdev`, value-length bounds. On a PII column that means actual phone numbers,
national IDs or names sit inside the contract YAML, which then leak the moment the contract is
shared with an external LLM (e.g. for enrichment). So **any automated scan that generates a
contract strips the whole `quality` block off every PII-flagged column.**

A column counts as PII when it carries any sign of it: a `privacy` block, `classification: pii_*`,
an `entity_type`, or a `pii` / `gdpr_personal_data` tag (the existing `column_is_pii` test). The
**table-level** quality (`rowCount`, column-set) is never touched — it holds no per-row values.

What you do instead: add custom quality rules by hand (`redibis rules` / the UI), or **mask** the
column first and let the masked copy feed LLM enrichment.

```yaml
# Before (leaky) — recipient_mobile, entity_type PHONE_NUMBER
- name: recipient_mobile
  entity_type: PHONE_NUMBER
  tags: [pii, gdpr_personal_data]
  quality:
    - {engine: greatExpectations, implementation: {expectation_type: expect_column_min_to_be_between,
        kwargs: {min_value: 1000049649, max_value: 1000049649, column: recipient_mobile}}}   # real number!
    - ...quantiles / median / mean / stdev with real numbers...

# After (automated scan) — quality removed, PII typing kept
- name: recipient_mobile
  entity_type: PHONE_NUMBER
  tags: [pii, gdpr_personal_data]
  classification: pii_personal
  privacy: {classification: pii_personal, masking_policy: {default_strategy: fake, reversible: false}}
```

### Scope — what strips, what doesn't

| Path | Strips PII quality? |
|---|---|
| `redibis scan … --automerge pii\|quality\|both` | **Yes** |
| `redibis runs merge <table> <run>` (and `merge --batch`) | **Yes** |
| Legacy Workflow A/B runners (`QualityRunner` / `PIIDetectionRunner`) | **Yes** |
| Agentic batch scan (reuses the scan automerge writer) | **Yes** |
| **Web UI merge** (run-merge button, approved basket, `patch_*` edits) | **No — unchanged** |
| Enrichment re-write, memory `record_review`, `redibis contract …` edits | **No — unchanged** |

This is the deliberate split: **manual mode is untouched** — in the UI you can still review and keep
quality rules on a PII column on purpose. Only the automated generators strip.

### Mechanism + config

The strip happens at the single writer, `ContractStore.upsert(..., strip_pii_quality=...)`, right
after the PII/quality/definition overlays are reconciled (so a column the overlay just marked PII is
also stripped, and one demoted to `not_pii` keeps its rules). The flag defaults to **False**;
automated callers pass `True`. Env overrides:

| Env var | Effect |
|---|---|
| `REDIBIS_KEEP_QUALITY_ON_PII=1` | Never strip, even on automated paths (full opt-out) |
| `REDIBIS_STRIP_PII_QUALITY_ALWAYS=1` | Strip on **every** write, including manual/UI (paranoid mode) |
| `REDIBIS_PII_QUALITY_KEEP_SAFE=1` | Keep value-free count rules (`missingCount`/`duplicateCount`), drop only the value-bearing ones |

Remediate an **already-written** contract without re-scanning — it self-heals on the next automated
merge, or do it explicitly:

```python
from redibis.contracts.privacy import strip_quality_from_pii_columns
c = store.get_active("eshop.order_header")
strip_quality_from_pii_columns(c)                      # returns ['recipient_mobile', ...]
store.upsert(c, table="eshop.order_header", workflow="manual", strip_pii_quality=True)
```

> Detection gap to know about: a column only strips if it's *flagged* PII. In the sample contract
> `recipient_name` held real names in `validValues` but was **not** detected as PII, so it slips
> through — flag it (UI Final Review / `set_pii_decision`) or improve detection and it strips on the
> next automated write.

---

## 7. Options cheat-sheet (what to flip for what)

| Goal | Set |
|---|---|
| PII + quality + profile | `--mode all` / `run_pii=run_quality=run_profile=True` |
| Faster, no GE docs | `--no-ge-docs` / `generate_ge_docs=False` |
| Lean metrics instead of GE rules | `--profiler-engine open_metadata` |
| Stricter/looser PII | `--equation strict\|lenient` / `equation_mode=...` |
| Air-gapped NER model | `--ner-model /opt/redibis/models/<name>` |
| Write to S3/MinIO | `--use-s3 --s3-endpoint http://minio:9000` |
| Merge into the live contract now | `--automerge both` (else `redibis runs merge ...` later) |
| Masked CSV (default) | `auto_suggest_plan(...)` → `MaskingEngine(...).transform_dataframe` |
| Masked CSV (override) | edit `plan.rule_for(col).strategy/params` before transform |
| Keep PII quality on automated scan | `REDIBIS_KEEP_QUALITY_ON_PII=1` (default: stripped — see §6b) |
| Strip PII quality everywhere (incl. UI) | `REDIBIS_STRIP_PII_QUALITY_ALWAYS=1` |

---

## 8. Invariants honored
- `ContractStore.upsert` is the only contract writer; `scan_csv`/automerge route through it.
- Reports/artifacts are evidence under the run dir / `_meta`; the contract YAML is the durable spec.
- Masking keys are **per-run**, stored under `session/_meta/env`, and **never** written into the
  exported CSV or manifest (a new run regenerates them).
- **No raw PII values in the contract:** automated scan generators strip value-bearing quality
  rules off PII columns at write time (§6b); manual/UI merges are exempt by design.
