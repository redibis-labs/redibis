# Data Masking & De-identification — Full Reference

Complete programmatic reference for `redibis.masking`: value-level de-identification
of tabular data before sharing, exporting, or downstream processing.

**Surfaces covered:** Python library, CLI, REST API.

**Package:** `redibis.masking`  
**Service layer:** `redibis.services.masking_service.MaskingService` (session-scoped; used by REST)

---

## Table of contents

1. [Overview](#1-overview)
2. [Core concepts](#2-core-concepts)
3. [Masking plan model](#3-masking-plan-model)
4. [Strategies](#4-strategies)
5. [Regex-based fake generation](#5-regex-based-fake-generation)
6. [Risk report](#6-risk-report)
7. [Export artifacts: manifest & audit report](#7-export-artifacts-manifest--audit-report)
8. [Python API](#8-python-api)
9. [CLI reference](#9-cli-reference)
10. [REST API reference](#10-rest-api-reference)
11. [Session storage layout](#11-session-storage-layout)
12. [Security notes](#12-security-notes)
13. [Extending](#13-extending)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Overview

A masking run transforms a DataFrame column-by-column according to a saved
**plan**, using per-run **keys** that are never shipped with the export.

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│ MaskingPlan │ ──► │ MaskingEngine│ ──► │ masked DataFrame│
│  (reusable) │     │  + RunKeys   │     │ + sidecar files │
└─────────────┘     └──────────────┘     └────────────────┘
                           │
                    keys → session/_meta/env (never exported)
                    sidecars → .manifest.json, .audit.json
```

**Typical workflows**

| Goal | Python | CLI | REST (session) |
|------|--------|-----|----------------|
| Build a plan from PII scan | `auto_suggest_plan(...)` | `redibis mask plan --from-pii` | `GET …/mask/plan` (auto on first load) |
| Preview one record | `compare_record(df, row)` | — | `POST …/mask/preview?row=` |
| Apply & export | `transform_dataframe` + write files | `redibis mask apply` | `POST …/mask/apply` |
| Audit trail | `build_audit_report(...)` | writes `.audit.json` | `GET …/mask/audit` |

CLI commands are **local-only** — they never touch S3 or the contract store.

---

## 2. Core concepts

| Object | Role | Lifetime |
|--------|------|----------|
| `MaskingPlan` | *What* to do — one `ColumnMaskRule` per column | Saved YAML/JSON; reusable across tables |
| `RunKeys` | *Secrets* — master key + seed; sub-keys via `key_ref` | Minted per apply; stored under `session/_meta/env`; **never exported** |
| `MaskingEngine` | Applies the plan to a DataFrame | Per run |

### Invariants

1. **Keys are per-run.** Each apply mints new keys unless you deliberately reuse
   `plan.seed` / `--seed`. Exports are not cross-linkable across runs unless keys
   are reused.
2. **Determinism is per rule.** `deterministic: true` (default) maps the same
   input value to the same output *within a run* — joins between masked tables
   survive. `deterministic: false` randomizes per cell.
3. **Sidecars contain references only.** Manifest and audit report include
   `keys.to_public()` (run id, key ref names, pointer to env path) — never seed
   or key bytes.

### Optional dependencies

| Feature | Requires | Check |
|---------|----------|-------|
| Rich fakers (EN/AR names) | `faker` | embedded pools fallback |
| Regex fake (`kind: regex`) full support | `rstr` | built-in regex generator fallback |
| Parquet export | `pyarrow` | CSV always available |
| AES-256-GCM encrypt | `cryptography` | fails closed when `require_authenticated_crypto: true` |
| FF3-1 FPE | `ff3` | falls back to keystream when not installed |

```bash
redibis mask capabilities          # CLI
GET /api/masking/capabilities      # REST
```

---

## 3. Masking plan model

### `ColumnMaskRule`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `column` | string | required | Column name |
| `include_in_scan` | bool | `true` | Active-column toggle (also drives scan scope in sessions) |
| `strategy` | string | `passthrough` | One of `STRATEGIES` (see §4) |
| `detected_entity` | string? | `null` | Carried from PII scan (informational) |
| `deterministic` | bool | `true` | Same input → same output within a run |
| `params` | object | `{}` | Strategy-specific parameters |
| `note` | string | `""` | Free-text note |

### `MaskingPlan`

| Field | Type | Description |
|-------|------|-------------|
| `schema_table` | string | Logical table id (e.g. `telecom.customers`) |
| `plan_id` | string | Stable plan identifier |
| `session_id` | string? | Owning session (when session-scoped) |
| `run_id` | string? | Last apply run id (set on export) |
| `seed` | string? | Optional reproducibility seed |
| `key_refs` | object | Key reference metadata (never key material) |
| `default_locale` | string | `default` \| `ar` (`en` / `mixed` accepted for back-compat) |
| `require_authenticated_crypto` | bool | Default `true` — fail closed without ff3/AES-GCM |
| `columns` | list | `ColumnMaskRule` entries |
| `source` | string | `auto-from-pii` \| `manual` \| `saved:<name>` |

### Example plan YAML

```yaml
schema_table: telecom.customers
default_locale: default
require_authenticated_crypto: true
columns:
  - column: full_name
    strategy: fake
    detected_entity: PERSON
    deterministic: true
    params:
      kind: name
      locale: ar
  - column: email
    strategy: fake
    detected_entity: EMAIL
    params:
      kind: email
      preserve:
        domain: true
  - column: national_id
    strategy: fpe
    detected_entity: NATIONAL_ID
    params:
      mode: ff3
      alphabet: digits
      key_ref: k1
  - column: msisdn
    strategy: fake
    params:
      kind: regex
      regex_library: eg_mobile
  - column: city
    strategy: passthrough
```

### Auto-suggest from PII (`suggest_rule`)

When `auto_suggest_plan` receives PII detections, entity types map to strategies:

| Entity (normalized) | Strategy | Notes |
|---------------------|----------|-------|
| `EMAIL`, `EMAIL_ADDRESS` | `fake` / `email` | Preserves domain |
| `PHONE`, `PHONE_NUMBER` | `fake` / `phone` | Preserves format + country code |
| `PERSON`, `NAME`, `PER` | `fake` / `name` | Locale-aware |
| `ADDRESS`, `LOCATION`, `GPE`, `LOC` | `fake` / `address` | |
| `NATIONAL_ID`, `SSN`, `ID`, `PASSPORT` | `fpe` / `ff3` + `digits` | |
| `CREDIT_CARD`, `IBAN`, `BANK_ACCOUNT` | `fpe` / `ff3` + `digits` | |
| `DATE`, `DOB`, `BIRTHDATE` | `fake` / `date` | ±30 day jitter |
| `COMPANY`, `ORG` | `fake` / `company` | |
| Other detected PII | `hash` / `sha256` + `hmac_key_ref` | Stable pseudonym |
| Not detected | `passthrough` | |

---

## 4. Strategies

Set per column via `ColumnMaskRule(strategy=..., params={...})`.

### 4.1 `passthrough`

Value copied unchanged. Risk report flags quasi-identifier column names
(`dob`, `zip`, `gender`, …) left as passthrough.

### 4.2 `redact`

Replace every value with `params.replacement` (default `null`).

### 4.3 `mask` — partial character masking

```yaml
params:
  keep_first: 0
  keep_last: 4
  mask_char: "*"
  preserve_length: true
```

Example: `010012345678` → `0100****5678`. Irreversible.

### 4.4 `hash` — pseudonymization

```yaml
params:
  algo: sha256
  hmac_key_ref: k1
  truncate: 12
  prefix: ""
```

With `hmac_key_ref`, the digest is keyed by a per-run sub-key. **Always set
`hmac_key_ref` for low-cardinality columns** — risk report flags deterministic
unsalted hashes on columns with &lt;10% distinct values as **high** severity.

### 4.5 `encrypt` — reversible, format-destroying

```yaml
params:
  key_ref: k1
```

- **AES-256-GCM** when `cryptography` installed → `g1:<b64>` tokens
- With `require_authenticated_crypto: true` (plan default), missing
  `cryptography` raises instead of silently falling back
- Legacy **HMAC-XOR** (`x1:<b64>`) when `require_authenticated_crypto: false`

Only a key-holder with the run's secret material can call `decrypt_value()`.

### 4.6 `fpe` — reversible, format-preserving

```yaml
params:
  mode: ff3                  # ff3 | keystream (default: ff3 when ff3 installed)
  alphabet: digits           # digits | alnum | custom string
  key_ref: k1
  tweak: ""                  # defaults to column name
  short_value_policy: error  # error | keystream | passthrough
```

Characters in the alphabet are encrypted via **NIST FF3-1** (when `ff3` installed);
separators pass through. Example: `4111-1111-1111-1111` encrypts 16 digits; dashes
preserved.

**FF3-1 domain constraints**

| alphabet | min in-alphabet chars | max |
|----------|----------------------|-----|
| `digits` | 6 | 56 |
| `alnum` | 4 | 32 |

Below minimum: `error` (default), `keystream` (legacy fallback per value), or
`passthrough`. Above maximum: always errors.

**Legacy `keystream` mode** (`mode: keystream`) — original HMAC modular-add cipher.
Risk report flags keystream FPE as **high** severity.

### 4.7 `fake` — realistic replacement values

`params.kind` selects the faker. `default_locale: default` means value-aware
resolution: Arabic-script values use Arabic Faker, otherwise English. For
locale-aware fake kinds (`name`, `address`, `company`), a column can override
this with `params.locale: default|ar|en` (the UI labels `ar` / `en` as
`faker_arabic` / `faker_english`).

| kind | Behaviour | Useful params |
|------|-----------|---------------|
| `name` | EN/AR Faker-backed names (embedded pools fallback) | `preserve.gender` |
| `phone` | Re-randomizes digits, keeps grouping + country code | `preserve.format`, `preserve.country_code` |
| `email` | New local-part `@` original domain; derives from faked **name column** when present (two-pass engine) | `preserve.domain` |
| `address` | Locale-aware Faker-backed address (embedded fallback) | — |
| `company` | Locale-aware Faker-backed company (embedded fallback) | — |
| `national_id` | Random digits, same count | — |
| `credit_card` / `iban` | Digit-shape randomization (not Luhn-valid) | — |
| `date` | Jitters parseable dates ±N days | `jitter_days` |
| `uuid` | Deterministic UUID-shaped value | — |
| `free_text` | `[redacted]` (default) or lorem | `redact: false` |
| `regex` | Generate value matching a regex — see §5 | `regex_library` or `regex_pattern` |

### 4.8 Position slicing

Add `start_index` / `end_index` to any strategy except `passthrough` / `redact`
to transform only a substring:

```yaml
strategy: fpe
params:
  alphabet: digits
  start_index: 4    # keep first 4 chars intact
```

---

## 5. Regex-based fake generation

Used with `strategy: fake`, `params.kind: regex`.

```yaml
strategy: fake
params:
  kind: regex
  regex_library: eg_mobile       # registered pattern
# or
  regex_pattern: "EMP-\\d{6}"    # inline (wins if both set)
```

### Pattern library resolution

1. Explicit dict passed by caller
2. `REDIBIS_REGEX_PATTERNS` env var → JSON file path
3. `./regex_patterns.json` in working directory
4. Packaged default (`redibis/masking/regex_patterns.json`)

Packaged patterns include: `eg_mobile`, `eg_national_id`, `us_ssn`, `us_phone`,
`email_simple`, `uuid_v4`, `iso_date`, `credit_card_visa`, `iban_eg`, `ipv4`,
`hex_color`, `sa_mobile`.

### Determinism

Within one run, the same `(column, original value)` always generates the same
fake when `deterministic: true`.

---

## 6. Risk report

```python
from redibis.masking.engine import risk_report

findings = risk_report(df, plan)
# [{"column", "severity", "issue", "detail"}, ...]
```

| Severity | Typical triggers |
|----------|------------------|
| **high** | Deterministic hash on low-cardinality column; legacy keystream FPE; unauthenticated encrypt fallback |
| **medium** | Quasi-identifier passthrough; un-redacted free text; mixed-mode FPE |
| **low** | Reversible encrypt/fpe (informational — keys not exported) |

REST: `GET /api/sessions/{sid}/mask/risk` → `{"risk": [...]}`  
Preview endpoint also embeds risk on each refresh: `POST …/mask/preview?row=`.

---

## 7. Export artifacts: manifest & audit report

Every **apply** writes three files:

| File | Description |
|------|-------------|
| `{table}_{run_id}.csv` or `.parquet` | Masked dataset |
| `{table}_{run_id}.manifest.json` | Technical manifest (v2) |
| `{table}_{run_id}.audit.json` | Compliance audit report (v1) |

CLI naming: `{out}`, `{out}.manifest.json`, `{out}.audit.json`.

### 7.1 Manifest v2 (`build_manifest`)

```json
{
  "redibis_masking_manifest": "v2",
  "schema_table": "telecom.customers",
  "run_id": "mask_20250612_143022",
  "created_at": "2025-06-12T14:30:22+00:00",
  "rows": 1500,
  "default_locale": "mixed",
  "require_authenticated_crypto": true,
  "keys": {
    "run_id": "mask_20250612_143022",
    "seed_ref": "session/_meta/env",
    "key_refs": ["k1"]
  },
  "columns": [
    {
      "column": "national_id",
      "strategy": "fpe",
      "detected_entity": "NATIONAL_ID",
      "deterministic": true,
      "params": {"mode": "ff3", "alphabet": "digits", "key_ref": "k1"},
      "mode": "ff3",
      "algorithm": "FF3-1 (AES-256)"
    }
  ],
  "capabilities": {"ff3": true, "aes_gcm": true, "faker": true, "rstr": true},
  "session_id": "abc123"
}
```

FPE columns include `mode` and `algorithm`. Encrypt columns include `algorithm`.

### 7.2 Audit report v1 (`build_audit_report`)

Self-contained compliance record; safe to archive alongside the export.

```json
{
  "redibis_masking_audit": "v1",
  "run_id": "mask_20250612_143022",
  "created_at": "2025-06-12T14:30:22+00:00",
  "session_id": "abc123",
  "schema_table": "telecom.customers",
  "source": {
    "label": "/path/to/customers.csv",
    "rows": 1500,
    "columns": 12,
    "column_names": ["full_name", "email", "..."]
  },
  "export": {
    "format": "csv",
    "filename": "telecom_customers_mask_20250612_143022.csv"
  },
  "summary": {
    "columns_total": 12,
    "columns_transformed": 8,
    "columns_passthrough": 4,
    "pii_columns_detected": 6,
    "by_strategy": {"fake": 4, "fpe": 2, "passthrough": 4, "hash": 2},
    "risk_high": 0,
    "risk_medium": 1,
    "risk_low": 3
  },
  "columns": [ "... same shape as manifest column entries ..." ],
  "risk_findings": [ "... full risk_report output ..." ],
  "keys": { "... keys.to_public() ..." },
  "manifest": { "... embedded manifest v2 ..." },
  "compliance_notes": [
    "Key material and seeds are NOT included in the export, manifest, or audit report.",
    "Reversible strategies (encrypt, fpe) can be reversed only by the key-holder ...",
    "Each apply mints new keys unless the plan seed is reused ..."
  ]
}
```

**Key material never appears** in export, manifest, or audit report.

---

## 8. Python API

### Public imports

```python
from redibis.masking import (
    MaskingPlan, ColumnMaskRule, STRATEGIES,
    auto_suggest_plan, suggest_rule,
    MaskingEngine, RunKeys, risk_report,
)
from redibis.masking.engine import build_manifest, build_audit_report
from redibis.masking import transforms as T
```

### End-to-end example

```python
import json
import pandas as pd

df = pd.read_csv("customers.csv")

# Build plan from PII detections
plan = auto_suggest_plan(
    "telecom.customers",
    list(df.columns),
    detections=[
        {"column": "full_name", "detected": True, "entity_type": "PERSON"},
        {"column": "email", "detected": True, "entity_type": "EMAIL"},
    ],
    default_locale="mixed",
)

# Or load saved plan
# plan = MaskingPlan.from_yaml(open("plan.yaml").read())

keys = RunKeys.mint(seed="my-seed")   # omit seed for random per-run keys
engine = MaskingEngine(plan, keys)
masked = engine.transform_dataframe(df)

# Preview one record (raw vs masked per field)
preview = engine.compare_record(df, row_index=0)
# preview["fields"] → [{column, entity, strategy, raw, masked}, ...]

# Multi-row sample (legacy batch compare)
batch = engine.compare_sample(df, rows=10)

# Risk before export
findings = risk_report(df, plan)

# Write export + sidecars
out = "customers_safe.csv"
masked.to_csv(out, index=False)
manifest = build_manifest(plan, keys, column_run_meta=engine.column_run_meta, rows=len(df))
audit = build_audit_report(
    plan, keys, df=df,
    column_run_meta=engine.column_run_meta,
    export_format="csv",
    export_filename="customers_safe.csv",
    source_label="customers.csv",
)
open(out + ".manifest.json", "w").write(json.dumps(manifest, indent=2))
open(out + ".audit.json", "w").write(json.dumps(audit, indent=2))
```

### Session service (REST backend)

```python
from redibis.services.masking_service import MaskingService

svc = MaskingService(session)
plan = svc.get_plan()                          # auto-suggest if no saved plan
svc.save_plan(plan)
preview = svc.preview_mask(row=0)              # single-record compare + risk
result = svc.apply_export(fmt="csv")           # export + manifest + audit
audit = svc.audit(run_id=result["run_id"])
history = svc.list_exports()                   # all exports in session
```

### Decrypt (key-holder only)

```python
key = T._derive_key(keys.master_key, "k1")
T.decrypt_value(token, key)                    # encrypt strategy
T.fpe_transform(masked, key, alphabet="digits", tweak="nid", decrypt=True)
```

---

## 9. CLI reference

All commands under `redibis mask` and `redibis data preview`.

### 9.1 `redibis data preview`

Print head rows of a local file (no masking).

```bash
redibis data preview data.csv [--rows 10]
```

Supports CSV and Parquet (`.parquet`, `.pq`).

### 9.2 `redibis mask plan`

Write a masking plan YAML.

```bash
redibis mask plan data.csv \
  [--table schema.table] \
  [--out plan.yaml] \
  [--from-pii] \
  [--locale default|ar|en|mixed] \
  [--column-faker full_name=faker_arabic]
```

| Flag | Description |
|------|-------------|
| `--table` | Logical table name (defaults from filename stem) |
| `--out` | Output path (default `plan.yaml`) |
| `--from-pii` | Run PII detection to auto-suggest column rules |
| `--locale` | Default faker locale (falls back to `masking.default_locale` from config; default config is `default`) |
| `--column-faker` | Per-column faker override for locale-aware fake kinds: `COLUMN=faker_arabic|faker_english|default` |

### 9.3 `redibis mask apply`

Apply a saved plan and write masked export + sidecars.

```bash
redibis mask apply data.csv \
  --plan plan.yaml \
  [--out data_safe.csv] \
  [--format csv|parquet] \
  [--seed SEED]
```

**Outputs:**

- `{out}` — masked data
- `{out}.manifest.json` — manifest v2
- `{out}.audit.json` — audit report v1

Stdout includes audit summary counts and high/medium risk findings.

### 9.4 `redibis mask auto`

PII-detect, suggest plan, and apply in one step.

```bash
redibis mask auto data.csv \
  [--table schema.table] \
  [--out data_safe.csv] \
  [--format csv|parquet] \
  [--locale default|ar|en|mixed] \
  [--column-faker full_name=faker_english] \
  [--seed SEED]
```

Same sidecar outputs as `apply`.

### 9.5 `redibis mask capabilities`

```bash
redibis mask capabilities [--json]
```

Returns installed optional features (`ff3`, `aes_gcm`, `faker`, `rstr`, …).

### 9.6 `redibis mask regex`

```bash
# List registered patterns
redibis mask regex list [--json]

# Generate sample strings
redibis mask regex test \
  [--library eg_mobile | --pattern 'EMP-\d{6}'] \
  [-n 5] \
  [--deterministic] \
  [--seed s1]
```

---

## 10. REST API reference

Dashboard auth is on by default. Session cookie + `X-CSRF-Token` on writes:
[`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl). The CLI and
Python library do not use dashboard sessions.

All session endpoints require an active scan session with uploaded data at
`session.data_path`. Base path: `/api/sessions/{session_id}`.

Global masking utilities (no session): `/api/masking/…`

### 10.1 Data preview

#### `GET /data/preview`

**Single record** (preferred):

```
GET /api/sessions/{sid}/data/preview?row=0
```

Response:

```json
{
  "row_index": 0,
  "total_rows": 1500,
  "fields": [
    {"column": "full_name", "value": "Ahmed Hassan"},
    {"column": "email", "value": "ahmed@example.com"}
  ],
  "columns": ["full_name", "email"],
  "rows": [["Ahmed Hassan", "ahmed@example.com"]]
}
```

**Multi-row table** (legacy / batch):

```
GET /api/sessions/{sid}/data/preview?rows=15&page=0&full=false
GET /api/sessions/{sid}/data/preview?rows=15&page=2&full=true
```

Response:

```json
{
  "columns": ["col_a", "col_b"],
  "rows": [["v1", "v2"], ["v3", "v4"]],
  "total_rows": 1500,
  "total_cols": 12,
  "page": 0,
  "page_size": 15,
  "full": false
}
```

### 10.2 Column scope

#### `GET /data/columns`

```json
{
  "columns": [
    {
      "column": "email",
      "include_in_scan": true,
      "detected_entity": "EMAIL",
      "strategy": "fake"
    }
  ]
}
```

#### `PATCH /data/columns`

```json
{"column": "email", "include_in_scan": false}
```

→ `{"column": "email", "include_in_scan": false}`

Toggling `include_in_scan` updates the masking plan and scan scope.

### 10.3 Masking plan

#### `GET /mask/plan`

```
GET /api/sessions/{sid}/mask/plan
GET /api/sessions/{sid}/mask/plan?regenerate=true
```

Returns full `MaskingPlan` as JSON dict. First call auto-suggests from PII scan
if no saved plan exists (`{session_dir}/mask_plan.yaml`).

#### `PUT /mask/plan`

Partial or full update:

```json
{
  "plan": { "...full MaskingPlan dict..." }
}
```

Or field-level:

```json
{
  "columns": [ {"column": "email", "strategy": "hash", "params": {...}} ],
  "seed": "my-seed",
  "default_locale": "default"
}
```

For locale-aware fake kinds, API/class callers can also set
`params.locale` to `default`, `ar`, `en`, `faker_arabic`, or
`faker_english`.

Returns saved plan dict.

### 10.4 Mask preview (single record)

#### `POST /mask/preview?row=0`

Applies current plan with **ephemeral preview keys** (not persisted).

Response:

```json
{
  "row_index": 0,
  "total_rows": 1500,
  "fields": [
    {
      "column": "full_name",
      "entity": "PERSON",
      "strategy": "fake",
      "raw": "Ahmed Hassan",
      "masked": "Sara Smith"
    }
  ],
  "strategies": {"full_name": "fake", "city": "passthrough"},
  "risk": [
    {"column": "dob", "severity": "medium", "issue": "...", "detail": "..."}
  ]
}
```

Errors: **400** with rule validation message (e.g. regex rule missing pattern).

#### `GET /mask/risk`

```json
{"risk": [ "... risk_report findings ..." ]}
```

Dataset-level risk for the current plan (not row-specific).

### 10.5 Apply & export

#### `POST /mask/apply?format=csv|parquet`

Runs full transform, mints and persists keys under `session/_meta/env/`,
writes export + manifest + audit to `session/masked/`.

Response:

```json
{
  "run_id": "mask_20250612_143022",
  "format": "csv",
  "rows": 1500,
  "export_path": "/path/to/session/masked/telecom_customers_mask_20250612_143022.csv",
  "manifest_path": "...manifest.json",
  "audit_path": "...audit.json",
  "manifest": { "... manifest v2 ..." },
  "audit": { "... audit v1 ..." }
}
```

#### `GET /mask/export?format=csv|parquet`

Downloads the latest export file for the plan's `run_id`. If none exists,
triggers apply first.

Returns: file stream (`application/octet-stream`).

#### `GET /mask/manifest`

Returns manifest v2 JSON for the latest export. **404** if no export yet.

#### `GET /mask/audit?run_id=`

Returns audit report v1 JSON. Omit `run_id` for latest export. **404** if missing.

#### `GET /mask/audit/download?run_id=`

Downloads `{table}_{run_id}.audit.json` file.

#### `GET /mask/exports`

Lists all exports in the session (newest first):

```json
{
  "exports": [
    {
      "run_id": "mask_20250612_143022",
      "created_at": "2025-06-12T14:30:22+00:00",
      "format": "csv",
      "rows": 1500,
      "export_filename": "telecom_customers_mask_20250612_143022.csv",
      "audit_path": "/path/to/...audit.json",
      "summary": {
        "columns_transformed": 8,
        "pii_columns_detected": 6,
        "risk_high": 0,
        "risk_medium": 1,
        "risk_low": 3
      }
    }
  ]
}
```

### 10.6 Global masking utilities

| Method | Endpoint | Body / params | Response |
|--------|----------|---------------|----------|
| GET | `/api/masking/capabilities` | — | `{ff3: bool, aes_gcm: bool, ...}` |
| GET | `/api/masking/regex-patterns` | — | `{patterns: [{name, label, regex, ...}]}` |
| POST | `/api/masking/regex-test` | `{pattern?, library?, n, deterministic, seed?}` | `{pattern, samples[], deterministic}` |

Regex test body example:

```json
{
  "library": "eg_mobile",
  "n": 3,
  "deterministic": true,
  "seed": "test-seed"
}
```

---

## 11. Session storage layout

When using REST / `MaskingService`:

```
{session_dir}/
  mask_plan.yaml              # saved MaskingPlan
  masked/
    {table}_{run_id}.csv      # export(s)
    {table}_{run_id}.manifest.json
    {table}_{run_id}.audit.json
  _meta/env/
    {run_id}.json             # RunKeys secret dict (NEVER export)
```

Each apply creates a new `run_id` and new key file. Previous exports and audit
files remain on disk for history (`GET …/mask/exports`).

---

## 12. Security notes

- **Keys live in `_meta/env`.** Deleting the session destroys ability to reverse
  `encrypt` / `fpe` columns.
- **Seed reuse** (`plan.seed`, `--seed`) makes runs reproducible **and**
  cross-linkable — use only when stable joins across exports are required.
- **Credit card fakes** are intentionally not Luhn-valid.
- **FF3-1** is the production FPE mode when `ff3` is installed; legacy keystream
  is retained for back-compat and short-value fallback only.
- **Install `redibis[mask]`** in production for AES-GCM, FF3-1, faker, rstr, pyarrow.
- **Audit reports** are designed for compliance archival — they document what was
  done without exposing secrets.

---

## 13. Extending

| Change | Where |
|--------|-------|
| New strategy | `MaskingEngine._transform_series`, `suggest_rule` |
| New faker kind | `transforms.fake_X()`, `MaskingEngine._fake_value`, `transforms.capabilities()` |
| New regex pattern | JSON library + `REDIBIS_REGEX_PATTERNS` env |
| New audit field | `build_audit_report()` in `redibis/masking/engine.py` |
| New REST endpoint | `redibis/webapp/backend.py` + `MaskingService` |

Domain packages must not import `redibis.cli`.

---

## 14. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Preview/apply returns 400 "set regex_pattern or regex_library" | `kind: regex` rule missing both params |
| Regex output wrong / truncated | Install `rstr`; verify with `redibis mask regex test --library <name>` |
| Same input differs across exports | Expected — keys are per-run. Reuse `--seed` / `plan.seed` for stability |
| Parquet export fails | Install `pyarrow` (`pip install redibis[mask]`) |
| Arabic names unrealistic | Install `faker` for richer `ar_AA` pools |
| FPE errors on short values | Value below FF3-1 minimum — adjust data or set `short_value_policy` |
| Audit 404 | No export yet — call `POST …/mask/apply` first |
| Manifest missing `mode` on old exports | v1 manifests pre-date v2 FPE metadata; treat as keystream |

**Test coverage:** `tests/test_masking.py`, `tests/test_masking_regex_gen.py`,
`tests/test_masking_dq.py`.

---

## Related documentation

- [`masking_guide.md`](masking_guide.md) — short index (links here)
- [`services_layer_guide.md`](services_layer_guide.md) — `MaskingService` in the services layer
- [`tutorials/PIPELINE_GUIDE.md`](tutorials/PIPELINE_GUIDE.md) — end-to-end pipeline context
