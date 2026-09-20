# Data Scanning — Full Reference (Quality & PII)

Complete programmatic reference for redibis scan workflows: **profiling**,
**quality validation** (Great Expectations), **PII detection**, rule
customization, report export, and contract generation.

**Surfaces covered:** Python library, CLI, REST API (no UI).

**Related docs:** [`ge_expectations_reference.md`](ge_expectations_reference.md),
[`quality_module_guide.md`](quality_module_guide.md),
[`data_masking.md`](data_masking.md)

---

## Table of contents

1. [Overview](#1-overview)
2. [Scan modes & pipeline](#2-scan-modes--pipeline)
3. [Configuration (`RedibisConfig`)](#3-configuration-redibisconfig)
4. [Quality rules](#4-quality-rules)
5. [PII detection](#5-pii-detection)
6. [Python API](#6-python-api)
7. [CLI reference](#7-cli-reference)
8. [REST API reference](#8-rest-api-reference)
9. [Discovery runs](#9-discovery-runs)
10. [Approved basket & contract merge](#10-approved-basket--contract-merge)
11. [Report & artifact export](#11-report--artifact-export)
12. [Contract views & decision overlays](#12-contract-views--decision-overlays)
13. [Storage layout](#13-storage-layout)
14. [Architectural invariants](#14-architectural-invariants)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Overview

A **scan** profiles tabular data, runs GE quality expectations, detects PII,
and produces ODCS partial contracts plus HTML/JSON reports.

```
┌──────────┐   ┌───────────┐   ┌─────────────────┐   ┌──────────────────┐
│ Profile  │ → │  Quality  │ → │  PII detection  │ → │ ODCS partials +  │
│ (GE/OM)  │   │ gatekeeper│   │ + equation      │   │ reports + S3     │
└──────────┘   └───────────┘   └─────────────────┘   └──────────────────┘
```

| Phase | Output |
|-------|--------|
| **Profile** | Column stats, triage scores, `interactive_review.html`, suggested GE rules |
| **Quality** | GE validation, `quality-report-*.html`, `quality_contract.yaml`, GE Data Docs |
| **PII** | Per-column detections, `pii_detections.json/html`, `pii_contract.yaml` |

**Preferred library entry:**

```python
from redibis import ProfileScan, QualityScan, PIIScan, RedibisConfig
from redibis.services.scan_service import ScanConfig, ScanService, to_scan_config
```

**Legacy runners** (pre-sampled parquet, Workflow A/B): `QualityRunner`,
`PIIDetectionRunner` — still supported; unified `Scan` / `ScanService` is preferred.

---

## 2. Scan modes & pipeline

### Mode matrix

Parsed by `redibis.scan_mode.parse_scan_mode()`:

| Mode | `run_profile` | `run_quality` | `run_pii` |
|------|---------------|---------------|-----------|
| `all` / `both` | ✓ | ✓ | ✓ |
| `quality` | ✓ (implied) | ✓ | ✗ |
| `pii` | ✗ | ✗ | ✓ |
| `profile` | ✓ | ✗ | ✗ |
| `pii,quality` | ✓ | ✓ | ✓ |
| `profile,pii` | ✓ | ✗ | ✓ |

CLI: `--mode` on `redibis scan`, or use aliases `redibis profile` / `redibis quality`.

REST session: `scan_mode` form field — `both` \| `quality` \| `pii` (maps to unified scan).

### Pipeline ownership

| Layer | Module | Role |
|-------|--------|------|
| Orchestrator | `redibis.scan.base.Scan` | Template method: profile → quality → PII in memory |
| Facades | `redibis.scan.facades` | `ProfileScan`, `QualityScan`, `PIIScan` |
| Service | `redibis.services.scan_service.ScanService` | DataFrame → S3 + subcontracts + automerge |
| Shared steps | `redibis.services.pipeline` | Single source of truth for profile / quality / PII |
| Quality phase | `redibis.scan.quality_phase` | Gatekeeper + ODCS partial |
| Reports | `redibis.scan.report_bundle.ReportBundle` | Flush HTML/YAML/JSON artifacts |
| Contracts | `redibis.scan.contract_writer.ScanContractWriter` | Persist subcontracts |

### Automerge

`contract.automerge` / `--automerge`:

| Value | Behaviour |
|-------|-----------|
| `none` | Write subcontracts only; manual merge required |
| `pii` | Merge PII partial into active contract after scan |
| `quality` | Merge quality partial |
| `both` | Merge both workflows |

Manual merge: `redibis runs merge <table> --kind pii|quality` or approved basket (§10).

---

## 3. Configuration (`RedibisConfig`)

Canonical YAML spine: `redibis.config.RedibisConfig`.

```bash
redibis config dump-default -o redibis.yaml
```

### Root keys

| Key | Default | Description |
|-----|---------|-------------|
| `table` | `""` | Logical table `schema.table` |
| `scan_types` | `[profile, quality, pii]` | Phases to run |
| `profiling` | see below | Profiler engine + triage |
| `quality` | see below | GE docs + rule set |
| `pii` | see below | Engines, equation, thresholds |
| `masking` | roles path, locale | Links to masking pipeline |
| `storage` | local/S3 buckets | Runs + contract buckets |
| `contract` | automerge, validate | Contract write behaviour |
| `report` | formats, output_dir | Artifact formats + base dir |

### `profiling`

| Key | Default | Description |
|-----|---------|-------------|
| `engine` | `great_expectations` | `great_expectations` \| `open_metadata` |
| `triage_threshold` | `0.0` | Minimum triage score (informational for PII routing) |
| `arabic_threshold` | `0.05` | Arabic script ratio flag |
| `openmetadata.*` | | OM profiler options |

### `quality`

| Key | Default | Description |
|-----|---------|-------------|
| `generate_ge_docs` | `true` | Copy GE Data Docs folder |
| `rule_set.name` | optional | Named saved rule set |
| `rule_set.rules[]` | `[]` | User GE rules (override profiler suggestions when non-empty) |

Rule shape:

```yaml
quality:
  generate_ge_docs: true
  rule_set:
    name: my_checks
    rules:
      - rule: expect_column_values_to_not_be_null
        column: email
        kwargs: {}
      - rule: expect_column_values_to_be_unique
        column: customer_id
        kwargs: {}
```

### `pii`

| Key | Default | Description |
|-----|---------|-------------|
| `engines` | `both` | `regex` \| `gliner` \| `ner` \| `llm` \| `both` |
| `equation_mode` | `independent` | `strict` \| `balanced` \| `lenient` \| `independent` |
| `thresholds.presidio_min` | `0.80` | Regex confidence floor |
| `thresholds.gliner_min` | `0.70` | NER confidence floor |
| `thresholds.llm_min` | `0.82` | LLM floor (when populated) |
| `thresholds.very_high_confidence_floor` | `0.90` | Balanced-mode shortcut |
| `ner.type` | `gliner` | NER backend type |
| `ner.model_path` | `""` | Local BYOM weights path |
| `ner.labels` | `[]` | Label override |
| `ner.device` | `cpu` | |
| `ner.threshold` | `0.3` | Backend internal threshold |
| `ner.always_run` | `false` | Force NER even when regex ≥ 0.80 |
| `models_dir` | `/models` | BYOM upload directory |
| `llm.enabled` | `false` | LLM refiner (limited in unified pipeline) |
| `regex_overrides` | null | Embedded regex pattern set |
| `selected_columns` | null | Limit scanned columns |

### `storage`

| Key | Default |
|-----|---------|
| `backend` | `local` |
| `runs_bucket` | `pii-reports` |
| `contracts_bucket` | `active-contracts` |
| `pii_runs_bucket` | `pii-contracts` |
| `quality_runs_bucket` | `quality-contracts` |

### `contract`

| Key | Default |
|-----|---------|
| `automerge` | `none` |
| `validate` | `true` |
| `auto_create` | `true` |

### `ScanConfig` (programmatic)

Built via `to_scan_config(cfg, table=..., session_id=..., ...)`. Notable fields:

| Field | Source |
|-------|--------|
| `run_profile`, `run_quality`, `run_pii` | `scan_types` or explicit |
| `quality_rule_set` | `quality.rule_set` or session override |
| `equation_mode`, `pii_engines`, thresholds | `pii.*` |
| `generate_ge_docs`, `validate_contracts` | quality / contract |
| `automerge` | `contract.automerge` |
| `artifacts_dir`, `session_id`, `run_id` | CLI/session layout |

---

## 4. Quality rules

### 4.1 Rule sources (precedence)

When the quality gatekeeper runs:

1. **Session `QualityRuleSet.rules`** — if non-empty, these rules win
2. **Profiler suggestions** — GE OnboardingDataAssistant / `ProfileResult.suggested_rules`
3. **Manual additions** — via discovery, paste-rules, or API

Implementation: `pipeline.apply_quality_rules()`.

### 4.2 Rule object shapes

**Session / config rule** (`QualityRuleSet`):

```json
{
  "rule": "expect_column_values_to_not_be_null",
  "column": "email",
  "kwargs": {"mostly": 0.95},
  "meta": {"notes": {"content": "Business rule"}}
}
```

**Paste-parser output** (`parse_ge_rules`):

```json
{
  "expectation_type": "expect_column_values_to_not_be_null",
  "column": "email",
  "kwargs": {},
  "meta": {}
}
```

Both flow into `quality_row_to_fragment()` for the approved basket and into
`QualityGatekeeper` for execution.

### 4.3 Generating rules (profiler)

The default GE profiler (`GreatExpectationsProfiler`) produces:

- Per-column structural profiles
- Arabic detection
- **Suggested expectations** (null checks, type checks, uniqueness, etc.)

Export curated rules from profiler output:

```python
profile = profiler.profile(df)
profile.export_interactive_review("interactive_review.html")  # human review → GE code
```

### 4.4 Pasting GE rule code (safe parse)

**Security boundary:** pasted Python is **never executed**. Parser uses
`ast.parse` + `ast.literal_eval` per argument only.

```python
from redibis.contracts.rule_code_parser import parse_ge_rules

out = parse_ge_rules(pasted_python_source)
# {"rules": [...], "errors": [{"line", "message", "snippet"}], "ignored": int}
```

Recognized patterns:

- `qa.add_gx_expectation(...)`
- `validator.expect_*`
- Bare `expect_*`

REST: `POST /api/quality/parse-rules` with `{"code": "..."}`.

### 4.5 Evaluating rules against sample data

REST: `POST /api/sessions/{sid}/quality/evaluate`

```json
{
  "code": "validator.expect_column_values_to_not_be_null('email')",
  "rules": [],
  "subset_rows": 500
}
```

Response per rule:

```json
{
  "expectation_type": "expect_column_values_to_not_be_null",
  "column": "email",
  "kwargs": {},
  "success": true,
  "element_count": 100,
  "unexpected_count": 0,
  "unexpected_percent": 0.0,
  "partial_unexpected": [],
  "observed_value": null
}
```

Summary: `{"total", "passed", "failed"}`.

Discovery quality runs use the same probe engine (`QualityProbe`).

### 4.6 Customizing rules (session API)

| Method | Path | Action |
|--------|------|--------|
| GET | `/api/sessions/{sid}/config` | Full session config |
| PATCH | `/api/sessions/{sid}/config` | Partial scalar update |
| PUT | `/api/sessions/{sid}/config/quality` | Replace `QualityRuleSet` |
| POST | `/api/sessions/{sid}/config/quality/rules` | Add one rule |
| DELETE | `/api/sessions/{sid}/config/quality/rules/{index}` | Remove by index |
| POST | `/api/sessions/{sid}/config/quality/load/{name}` | Load named registry config |

Named quality configs (global registry):

| Method | Path |
|--------|------|
| GET | `/api/configs/quality` |
| GET | `/api/configs/quality/{name}` |
| POST | `/api/configs/quality` |
| DELETE | `/api/configs/quality/{name}` |

### 4.7 GE → ODCS mapping

`redibis.contracts.mapper.ge_expectation_to_odcs` converts GE expectations to
ODCS quality blocks for contract partials.

### 4.8 Exporting rules from active contract

CLI:

```bash
redibis rules list telecom.customers
redibis rules export telecom.customers --target ge      # default
redibis rules export telecom.customers --target sodacl
redibis rules export telecom.customers --target dbt
```

REST:

```
GET /api/contracts/{table}/rules
GET /api/contracts/{table}/rules/export?target=ge|sodacl|dbt
```

Targets map to GE suite JSON, SodaCL YAML, or dbt schema fragments via
`redibis.contracts.rules.regenerate()`.

---

## 5. PII detection

### 5.1 Two-stage model

| Stage | Component | Output |
|-------|-----------|--------|
| Evidence | `PIIDetector.detect_pii()` | Scores, patterns, labels — **`detected` always False** |
| Verdict | `EquationEngine.decide_pii()` | Sets `detected=True/False`, confidence, decision path |

Unified entry: `pipeline.run_pii_detection(df, ...)`.

### 5.2 Engines (`pii.engines`)

| Value | Regex (Presidio) | NER (GLiNER/BYOM) |
|-------|------------------|-------------------|
| `both` | ✓ | ✓ |
| `regex` | ✓ | ✗ |
| `gliner` / `ner` | ✗ | ✓ |
| `llm` | Config-valid; **not invoked** in unified `detect_pii()` today |

**NER skip:** unless `ner.always_run` / `pii_gliner_always_run`, NER is skipped
when regex score ≥ **0.80**.

**Sampling:** up to **100** random non-null values per column.

### 5.3 Equation modes

| Mode | Rule |
|------|------|
| `strict` | All available engines must pass their threshold |
| `balanced` | ≥2 engines pass **OR** any engine ≥ `very_high_confidence_floor` (0.90) |
| `lenient` | Any single engine passes |
| `independent` (default) | Per-engine OR — same as lenient; avoids low scores dragging others down |

### 5.4 Thresholds

| Config key | Default (YAML) | Web session default |
|------------|----------------|---------------------|
| `thresholds.presidio_min` | 0.80 | 0.80 |
| `thresholds.gliner_min` | 0.70 | 0.40 |
| `thresholds.llm_min` | 0.82 | 0.82 |

### 5.5 `PIIDetection` result fields

| Field | Meaning |
|-------|---------|
| `column`, `entity_type`, `classification` | Column + detected type |
| `detected`, `confidence`, `equation_used` | Final verdict |
| `presidio_score`, `presidio_pattern`, `presidio_match_rate` | Regex engine |
| `gliner_score`, `gliner_label`, `ner_engine` | NER engine |
| `arabic_aware`, `triage_score` | Context (triage does not drive verdict) |
| `decision_path` | Human-readable trace |

Formal per-column report: `build_column_report()` → `pii_report.json`.

### 5.6 Regex overrides

Session API:

| Method | Path |
|--------|------|
| PUT | `/api/sessions/{sid}/config/regex` |
| POST | `/api/sessions/{sid}/config/regex/patterns` |
| DELETE | `/api/sessions/{sid}/config/regex/patterns/{key}` |
| POST | `/api/sessions/{sid}/config/regex/load/{name}` |

Global registry: `/api/configs/regex`, `/api/regex-catalog`.

### 5.7 BYOM NER models

Full guide: [`ner_models.md`](ner_models.md) (download from Hugging Face, package,
upload, Docker mounts).

CLI:

```bash
redibis models list [--json] [--models-dir PATH] [--config redibis.yaml]
redibis models upload archive.zip --name my_ner [--models-dir PATH]
redibis models activate my_ner [--config redibis.yaml]
redibis models delete my_ner [-y]
```

Scan flags: `--ner-model PATH` (alias `--gliner-model`).

REST: `/api/models`, `/api/sessions/{sid}/models/activate`.

Optional manifest beside weights: `redibis-model.json`.

### 5.8 PII decision overlay (not basket)

Removing PII from a column **must** go through the overlay — merger only unions tags.

CLI:

```bash
redibis contract strip-pii telecom.customers --column email
redibis contract add-pii telecom.customers --column notes --entity-type PERSON --confidence 1.0
```

REST:

```
POST /api/contracts/{table}/columns/{col}/strip-pii
POST /api/contracts/{table}/columns/{col}/add-pii
DELETE /api/contracts/{table}/columns/{col}/pii-decision
PATCH  /api/contracts/{table}/columns/{col}/privacy
```

---

## 6. Python API

### 6.1 Facades (preferred)

```python
import pandas as pd
from redibis import QualityScan, PIIScan, ProfileScan
from redibis.services.scan_service import ScanConfig

df = pd.read_csv("customers.csv")
cfg = ScanConfig(table="telecom.customers", run_pii=False, run_quality=True)

# Profile only
pr = ProfileScan(cfg).run(df, run_dir="./out/run1")

# Quality (profile + GE)
qr = QualityScan(cfg).run(df, run_dir="./out/run2")
print(qr.quality_passed, qr.quality_expectations)

# PII only
pir = PIIScan(cfg).run(df, run_dir="./out/run3")
print(pir.flagged_columns)
```

Flush artifacts:

```python
from redibis.scan.report_bundle import ReportBundle
ReportBundle.from_result(scan_run_result, cfg).flush(run_dir)
```

### 6.2 From YAML config

```python
from redibis import RedibisConfig
from redibis.services.scan_service import to_scan_config, ScanService

cfg = RedibisConfig.from_yaml("redibis.yaml")
scan_cfg = to_scan_config(cfg, table="telecom.customers")
result = ScanService(backend, store, runs_bucket="pii-reports").scan_dataframe(df, scan_cfg)
print(result.artifacts, result.quality_passed, result.pii_columns_detected)
```

### 6.3 Quality components (direct)

```python
from redibis.profiling import get_profiler
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.contracts.rule_code_parser import parse_ge_rules

profiler = get_profiler("great_expectations", config)
profile = profiler.profile(df)

gk = QualityGatekeeper(table="t", df=df)
gk.add_expectations_from_rules(rules)
results = gk.validate()
partial = gk.export_quality_contract()
```

### 6.4 PII components (direct)

```python
from redibis.services import pipeline

detections = pipeline.run_pii_detection(
    df,
    engines="both",
    equation_mode="independent",
    regex_confidence=0.80,
    gliner_confidence=0.40,
)
flagged = [d for d in detections if d.detected]
```

### 6.5 CLI-equivalent session

```python
from redibis.services.code_scan_session import CodeScanSession

session = CodeScanSession(output_root="./reports", table="telecom.customers", ...)
result = session.scan(df, config=scan_cfg, automerge="none")
```

---

## 7. CLI reference

### 7.1 Scan commands

| Command | Default mode | Description |
|---------|--------------|-------------|
| `redibis scan <file> [table]` | `all` | Unified scan |
| `redibis profile <file> [table]` | `profile` | Profile only |
| `redibis quality <file> [table]` | `quality` | Profile + GE validation |

#### Shared flags

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | per command | `all`, `profile`, `pii`, `quality`, or comma list |
| `--automerge` | `none` | `none` \| `pii` \| `quality` \| `both` |
| `--equation` | `independent` | PII equation mode |
| `--pii-engines` | `both` | `regex` \| `gliner` \| `ner` \| `llm` \| `both` |
| `--ner-model` | `""` | BYOM NER path (`--gliner-model` alias) |
| `--no-ge-docs` | off | Skip GE Data Docs copy |
| `--no-validate` | off | Skip ODCS validation on writes |
| `--session-id` | new UUID | Reuse session directory |
| `--scan-output-dir` | `--output-dir` | Session root |
| `--config` | — | Load `redibis.yaml` |
| `--profiler-engine` | from config | `great_expectations` \| `open_metadata` |
| `--output-dir` | `./reports` | Local output root |
| `--use-s3` | off | S3 vs local `_dev_storage` |
| `--s3-endpoint`, `--s3-*-bucket` | see defaults | Storage overrides |

#### Examples

```bash
# Full scan, no automerge
redibis scan customers.csv --table telecom.customers --output-dir ./reports

# Quality only with config file
redibis quality customers.csv --config redibis.yaml --automerge quality

# PII only, strict equation, custom NER
redibis scan customers.csv --mode pii --equation strict \
  --ner-model /models/my_gliner --pii-engines ner

# PII + quality, automerge both
redibis scan customers.csv --mode pii,quality --automerge both --use-s3
```

### 7.2 Run subcontracts

```bash
redibis runs list telecom.customers --kind quality
redibis runs list telecom.customers --kind pii

redibis runs merge telecom.customers --kind quality [--run RUN_ID] [--no-validate]
redibis runs discard telecom.customers --kind pii --run RUN_ID
```

### 7.3 Quality rules (contract)

```bash
redibis rules list telecom.customers
redibis rules export telecom.customers --target ge
redibis rules export telecom.customers --target sodacl
redibis rules export telecom.customers --target dbt
```

### 7.4 Approved basket

```bash
redibis approved list --session SESSION_ID
redibis approved add-pii --session SESSION_ID --from-json row.json [--column col]
redibis approved add-quality --session SESSION_ID --from-json rule.json [--column col]
redibis approved preview --session SESSION_ID
redibis approved merge --session SESSION_ID [--no-validate]
redibis approved clear --session SESSION_ID [--only-merged]
redibis approved show --session SESSION_ID --prop-id ID
redibis approved remove --session SESSION_ID --prop-id ID
```

### 7.5 Contract helpers

```bash
redibis contract pii-view telecom.customers [--json]
redibis contract quality-view telecom.customers [--json]
redibis contract metadata telecom.customers
redibis contract export-package telecom.customers

redibis contract quality-suppress telecom.customers --rule-id RULE_ID [--column COL]
redibis contract quality-restore telecom.customers --rule-id RULE_ID

redibis contract strip-pii telecom.customers --column email
redibis contract add-pii telecom.customers --column notes --entity-type PERSON
```

### 7.6 Config & models

```bash
redibis config dump-default [-o redibis.yaml]
redibis models list|upload|activate|delete  # see §5.7
```

---

## 8. REST API reference

Base: `/api/sessions/{session_id}` unless noted. Requires uploaded data unless
stateless (`/api/quality/parse-rules`).

Dashboard auth is **on by default**. Unauthenticated calls return
`{"detail":"authentication required"}`. Sign in with a cookie jar and send
`X-CSRF-Token` on POST/PUT/PATCH/DELETE —
[`DASHBOARD_AUTH.md` — Calling the API](DASHBOARD_AUTH.md#calling-the-api-curl).
The CLI and Python library do not use dashboard sessions.

### 8.1 Session lifecycle

#### `POST /api/sessions`

Multipart upload + scan configuration.

| Form field | Default | Description |
|------------|---------|-------------|
| `file` | required | CSV/Parquet |
| `table` | from filename | `schema.table` |
| `scan_mode` | `both` | `pii` \| `quality` \| `both` |
| `equation` | `independent` | PII equation |
| `pii_engines` | `both` | |
| `pii_regex_confidence` | `0.80` | |
| `pii_gliner_confidence` | `0.40` | |
| `pii_llm_confidence` | `0.82` | |
| `pii_gliner_model` | `""` | NER path |
| `pii_models_dir` | `""` | |
| `pii_gliner_always_run` | `false` | |
| `selected_columns` | JSON list or null | Column subset |
| `pii_regex_config` | | Named regex config |
| `automerge` | `none` | |
| `quality_config` | | Optional quality overrides |

Response: `{session_id, status, common_config}`.

#### `POST /api/sessions/{sid}/scan`

Starts background unified scan. Returns `{status: "started", session_id, scan_mode}`.

#### `GET /api/sessions/{sid}`

Full session state including `pii_detections`, `quality_passed`, `artifacts`, `runs`, `approved`, `discovery`.

#### `GET /api/sessions/{sid}/stream`

Server-sent events log stream.

#### `GET /api/sessions/{sid}/artifacts/{key}`

Serve artifact file by key (`quality_report`, `pii_detections`, `interactive_review`, …).

#### `GET /api/sessions/{sid}/sample`

Sample rows from session data.

#### `GET /api/scan_output/{session_id}`

Load persisted `session.json` from disk.

### 8.2 Session config

#### `GET /api/sessions/{sid}/config`

Returns `GlobalConfig.to_dict()`.

#### `PATCH /api/sessions/{sid}/config`

```json
{"fields": {"equation_mode": "strict", "pii_gliner_confidence": 0.5}}
```

Patchable scalars include: `scan_mode`, `equation_mode`, `pii_engines`,
thresholds, `selected_columns`, `automerge`, `generate_ge_docs`, `validate_contracts`,
LLM fields, NER model paths, `triage_threshold`.

### 8.3 Quality rules (session)

See §4.6 for full table.

#### `POST /api/quality/parse-rules` (stateless)

```json
{"code": "validator.expect_column_values_to_not_be_null('email')"}
```

→ `{rules[], errors[], ignored, count}`.

#### `POST /api/sessions/{sid}/quality/evaluate`

See §4.5.

### 8.4 PII config (session)

Regex editing: §5.6.

Column scope:

```
GET  /api/sessions/{sid}/data/columns
PATCH /api/sessions/{sid}/data/columns  {"column", "include_in_scan"}
```

### 8.5 Risk (quality)

```
GET /api/sessions/{sid}/mask/risk          # masking risk (separate module)
GET /api/sessions/{sid}/mask/preview?row=  # see data_masking.md
```

Quality risk is embedded in evaluate results and gatekeeper output; contract-level triage:

```
GET /api/contracts/{table}/triage
```

---

## 9. Discovery runs

Scoped, **non-contract** probes for tuning rules and PII thresholds before merge.

### `POST /api/sessions/{sid}/discovery/pii`

Body (`DiscoveryRunBody`):

| Field | Description |
|-------|-------------|
| `columns` | Subset; null/[] = all |
| `subset_rows` | Row cap |
| `background` | Async if true |
| `engines` | Override `pii_engines` |
| `gliner_model` | NER path override |
| `regex_patterns`, `regex_replace_all` | Ad-hoc regex set |
| `regex_confidence`, `gliner_confidence`, `llm_confidence` | Threshold overrides |
| `equation_mode` | Equation override |

### `POST /api/sessions/{sid}/discovery/quality`

Same body shape; `rules` array for explicit GE rules to probe.

### `GET /api/sessions/{sid}/discovery`

All discovery runs: `{discovery_id, runs[], run_count}`.

### `GET /api/sessions/{sid}/discovery/runs/{run_id}`

Single run:

```json
{
  "run_id": "...",
  "kind": "pii",
  "status": "complete",
  "results": [ "... detection or evaluate rows ..." ],
  "summary": {"columns_scanned": 10, "detected": 3},
  "config_snapshot": { "... effective GlobalConfig ..." }
}
```

Discovery writes **no contract files** — results are in-memory / `discovery.json`.

| Aspect | Full scan | Discovery |
|--------|-----------|-----------|
| Contracts | Subcontracts + optional automerge | None |
| Artifacts | ReportBundle files | JSON in session only |
| Use case | Production ODCS generation | Interactive tuning |

---

## 10. Approved basket & contract merge

Curated PII columns and quality rules staged for manual merge into the active contract.

### Flow

```
scan / discovery / evaluate / parse-rules
  → POST .../approved/pii | .../approved/quality
  → GET  .../approved/preview   (build_approved_partials)
  → POST .../approved/merge     (merge_approved → ContractStore.upsert)
```

**Invariant:** approved basket never writes contracts directly — only via `merge_approved`.

### REST

| Method | Path | Body |
|--------|------|------|
| GET | `/api/sessions/{sid}/approved` | — |
| POST | `/api/sessions/{sid}/approved/pii` | `{column, detection}` |
| POST | `/api/sessions/{sid}/approved/quality` | `{column?, rule, source_run_id?, note?}` |
| GET | `/api/sessions/{sid}/approved/preview` | `{partials: {pii, quality}, summary}` |
| POST | `/api/sessions/{sid}/approved/merge` | `{validate_contract: true}` |
| GET/PUT/DELETE | `/api/sessions/{sid}/approved/{prop_id}` | Property CRUD |
| DELETE | `/api/sessions/{sid}/approved` | `?only_merged=false` |

Merge response: `{status: "merged", merged_version, results, summary}`.

### Session subcontracts (alternative staging)

| Method | Path |
|--------|------|
| GET/POST/PUT/DELETE | `/api/sessions/{sid}/subcontracts` (`kind: "pii"` \| `"quality"`) |
| POST | `/api/sessions/{sid}/subcontracts/merge` |

### Run bucket merge (S3 subcontracts)

| Method | Path |
|--------|------|
| GET | `/api/contracts/{table}/runs?kind=pii\|quality` |
| GET/PATCH | `/api/runs/{kind}/{table}/{run_id}` |
| POST | `/api/runs/{kind}/{table}/{run_id}/merge` |
| POST | `/api/runs/{kind}/{table}/{run_id}/discard` |

---

## 11. Report & artifact export

### 11.1 `ReportBundle` artifact keys

Written under `{session}/runs/{run_id}/artifacts/` (CLI) or session root (web).

| Key | File | Phase | Format |
|-----|------|-------|--------|
| `interactive_review` | `interactive_review.html` | Profile | HTML — curate GE rules |
| `triage_report` | `triage_report.html` | Profile | HTML — column triage |
| `quality_report` | `quality-report-{run_id}.html` | Quality | HTML — validation summary |
| `ge_report` | `ge_report/index.html` | Quality | GE Data Docs tree |
| `quality_contract` | `quality_contract.yaml` | Quality | ODCS partial |
| `pii_contract` | `pii_contract.yaml` | PII | ODCS partial |
| `pii_detections` | `pii_detections.json` | PII | Raw detection objects |
| `pii_detections_html` | `pii_detections.html` | PII | HTML detection report |
| `pii_report` | `pii_report.json` | PII | Formal column reports |
| `pii_regex_review` | `pii_regex_review.html` | PII | Regex override review |
| `run_report` | `run_report.html` | Session | Dashboard with artifact links |

S3 upload (when configured): keys prefixed `s3:` in `ScanResult.artifacts`.

### 11.2 Run manifest

`runs/{run_id}/run_manifest.json` — scan summary dict (`ScanResult.to_dict()`):

```json
{
  "run_id": "...",
  "table": "telecom.customers",
  "status": "success",
  "total_rows": 1500,
  "quality_expectations": 42,
  "quality_passed": 38,
  "quality_failed": 4,
  "pii_columns_scanned": 12,
  "pii_columns_detected": 5,
  "quality_contract_version": "3",
  "pii_contract_version": "2",
  "artifacts": {"quality_report": "...", "pii_detections": "..."},
  "scan_started_at": "...",
  "scan_completed_at": "..."
}
```

### 11.3 Contract export formats

Full contract export (not scan-only):

```python
from redibis.contracts.exporter import export_contract

export_contract(contract_dict, "sodacl")
export_contract(contract_dict, "great-expectations")
export_contract(contract_dict, "dbt-sources")
```

CLI: `redibis contract export-package <table>` (bundle with metadata).

### 11.4 Legacy Workflow outputs

**QualityRunner** (`quality/run_outputs.py`):

```
{output}/{table}/{run_id}/
  ge_project/
  ge_report/
  data_contract.yaml
```

**PIIDetectionRunner** (`pii/run_outputs.py`):

```
pii/{table}/{run_id}/
  data_contract.yaml
  pii_summary.csv
  pii_detections.json
  pii_report.json
  sample_preview.parquet
  ge_report/          # optional
```

---

## 12. Contract views & decision overlays

Read-only slices from active contract:

| CLI | REST |
|-----|------|
| `redibis contract pii-view <table>` | `GET /api/contracts/{table}/pii-view` |
| `redibis contract quality-view <table>` | `GET /api/contracts/{table}/quality-view` |
| `redibis contract metadata <table>` | `GET /api/contracts/{table}/metadata` |
| `redibis show <table>` | `GET /api/contracts/{table}` |

Quality rule suppress/restore (overlay — merger cannot remove rules):

```
POST /api/contracts/{table}/quality-decisions/{rule_id}/suppress
POST /api/contracts/{table}/quality-decisions/{rule_id}/restore
POST /api/contracts/{table}/quality-decisions/manual
```

PII overlay: §5.8.

---

## 13. Storage layout

### Local CLI session

```
{output_dir}/{session_id}/
  data.csv
  session.json
  session_config.yaml
  approved.json
  discovery.json
  mask_plan.yaml                    # if masking used
  runs/{run_id}/
    run_manifest.json
    config_snapshot.json
    artifacts/
      interactive_review.html
      quality_contract.yaml
      pii_detections.json
      ...
```

### S3 buckets

| Bucket | Content |
|--------|---------|
| `{runs_bucket}` / `pii-reports` | Run artifacts, `run_manifest.json` |
| `{pii_runs_bucket}` / `pii-contracts` | PII subcontracts `{table}/{run_id}.yaml` |
| `{quality_runs_bucket}` / `quality-contracts` | Quality subcontracts |
| `{contracts_bucket}` / `active-contracts` | Merged active + audit trail |

Prefix pattern (unified scan): `scan/{table_safe}/{session_id}/{run_id}/`.

---

## 14. Architectural invariants

1. **`ContractStore.upsert()`** is the only writer to the contracts bucket.
2. **Detector vs equation:** `detect_pii()` never sets `detected=True`; `decide_pii()` does.
3. **Paste-rules boundary:** pasted Python parsed with AST only — never `exec`/`eval`.
4. **Approved basket** assembles partials then merges via `ContractStore.upsert()` — no direct bucket writes.
5. **PII removal** via decision overlay (`strip-pii` / `PiiDecisionStore`), not manual tag edits.
6. **Quality suppression** via `quality_decisions` overlay — merger unions rules only.
7. **Domain code must not import `redibis.cli`.**

---

## 15. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Parse-rules returns empty `rules` | Unrecognized GE call pattern; check `errors[]` |
| Evaluate 400 "No data" | Session has no uploaded CSV |
| Quality rules not from profiler | Non-empty `quality.rule_set.rules` overrides suggestions |
| NER never runs | Regex score ≥ 0.80 and `always_run` false |
| `llm` engine has no effect | Unified pipeline does not call LLM detector yet |
| Automerge did nothing | Check `contract.automerge` / `--automerge`; validation may fail |
| GE Data Docs missing | `--no-ge-docs` or in-memory GE context |
| Parquet input fails | Install `pyarrow` |
| Identity conflict on merge | Table identity locked — same `contract_uuid` required |
| PII can't be removed by editing YAML | Use `strip-pii` / decision overlay |

**Test coverage:** `tests/test_quality_scan_integration.py`,
`tests/test_cli_pii_scan.py`, `tests/test_rule_code_parser.py`,
`tests/test_merge_approved.py`, `tests/test_build_approved_partials.py`.

**Jupyter tutorial:** a runnable `data_scanning.ipynb` companion may ship with
your install or training materials; this markdown page is the canonical
reference.

---

## 12. Continuous quality monitoring

After rules are merged into the **active contract**, use **`redibis quality-monitor`** for
validate-only re-runs (no re-profiling, no `ContractStore.upsert()`):

```bash
redibis quality-monitor run telecom.customers --sample ./sample.csv --json
redibis quality-monitor batch --all-contracts --config redibis.yaml --json
redibis quality-monitor export telecom.customers -o ./packages
redibis quality-monitor airflow generate --all-contracts -o ./dags
```

`redibis quality-monitor export --python-file ./edited_quality.py` rebuilds a
package from a generated program you extended in Jupyter (parsed AST/literal-only,
never executed).

Web UI: **Copy Jupyter Code** (`POST /api/sessions/{sid}/quality/full-code`) and
**Download monitor package** (`POST /api/sessions/{sid}/quality/export-package`)
on the quality review and Data quality pages.

Artifacts: `monitor/{table}/{run_id}/quality_results.json` (MinIO or local `_dev_storage`).
Optional OpenMetadata test results when `catalog.push.quality: true`.

Full operator guide: [`cli/monitor.md`](cli/monitor.md) · [`QUALITY_SCAN_ALL_WAYS.md`](QUALITY_SCAN_ALL_WAYS.md) §6.

---

## Related documentation

| Doc | Topic |
|-----|-------|
| Data scanning (this guide) | Markdown reference for the scan → contract workflow |
| [`data_masking.md`](data_masking.md) | De-identification after PII scan |
| [`quality_module_guide.md`](quality_module_guide.md) | QualityProfiler + Gatekeeper internals |
| [`ge_expectations_reference.md`](ge_expectations_reference.md) | GE expectation catalogue |
| [`services_layer_guide.md`](services_layer_guide.md) | ScanService, session layer |
| [`CLI_QUALITY_SCAN_TUTORIAL.md`](tutorials/CLI_QUALITY_SCAN_TUTORIAL.md) | Hands-on quality CLI walkthrough |
| [`cli/monitor.md`](cli/monitor.md) | Continuous validate-only monitoring |
| [`CLI_SCAN_TUTORIAL.md`](tutorials/CLI_SCAN_TUTORIAL.md) | General scan CLI tutorial |
| [`TEXT_PII_EVAL.md`](TEXT_PII_EVAL.md) | Free-text span evaluation (strict vs value F1) |
| [`tutorials/TEXT_PII_EVAL_TUTORIAL.md`](tutorials/TEXT_PII_EVAL_TUTORIAL.md) | Hands-on span eval (CLI / UI / API) |
| [`tutorials/PIPELINE_GUIDE.md`](tutorials/PIPELINE_GUIDE.md) | Production S3 pipeline |
