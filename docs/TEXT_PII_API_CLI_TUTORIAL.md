# Free-Text PII Scan — API & CLI Tutorial

How to find PII **inside an arbitrary string** (notes, tickets, chat logs) and
optionally **de-identify** it. This path is separate from column/table scanning:
one string in, many spans out (`start` / `end` character offsets).

**Related:** [`TEXT_PII_SCAN.md`](TEXT_PII_SCAN.md),
[`TEXT_PII_EVAL.md`](TEXT_PII_EVAL.md) (span scoring),
[`tutorials/TEXT_PII_EVAL_TUTORIAL.md`](tutorials/TEXT_PII_EVAL_TUTORIAL.md),
[`FREE_TEXT_PII_IMPLEMENTATION_PLAN.md`](FREE_TEXT_PII_IMPLEMENTATION_PLAN.md),
[`TEXT_PII_SCAN_DESIGN.md`](TEXT_PII_SCAN_DESIGN.md).

---

## 1. What this feature does

| Goal | Tool |
|------|------|
| Detect PII spans in free text | REST `POST /api/pii/text/scan` or `redibis pii text` |
| Score Gateway accuracy on labeled spans | [`TEXT_PII_EVAL.md`](TEXT_PII_EVAL.md) · [`tutorials/TEXT_PII_EVAL_TUTORIAL.md`](tutorials/TEXT_PII_EVAL_TUTORIAL.md) |
| Score table columns vs engine / contract / LLM | Data → Evaluation or `redibis eval run` |
| List detectible entity types | REST `GET /api/pii/text/entities` |
| Check which engines are loaded | REST `GET /api/pii/text/health` |
| List named de-id policies | REST `GET /api/pii/text/policies` |
| Scan + mask/redact/hash/fake/FPE | REST `POST /api/pii/text/deidentify` or `redibis pii deid` |

**Properties that matter in practice:**

- **Stateless** — input is processed in memory; nothing is written to the contracts bucket.
- **Offsets are Unicode code points** — `offset_unit` is always `"unicode_codepoint"`.
  In Python, `text[start:end]` equals the matched span. In JavaScript, walk with
  `Array.from(text)`, not UTF-16 string indexes.
- **Fail-closed de-id** — without a policy, `/deidentify` and `pii deid` refuse to run
  (they never silently pass PII through).
- **Keys never leave** — FPE/hash run keys appear only as `run_key_ref`, never as key bytes.
- **LLM findings are proposals** — spans with `"is_proposal": true` come from optional
  local LLM refinement and are marked distinctly.

```
text ──► regex (Presidio patterns)
     ──► phone (libphonenumber gate)
     ──► NER (GLiNER, if model loaded)
     ──► LLM (optional, local, off by default)
              │
              ▼
         SpanResolver (overlap: priority | longest | all)
              │
              ▼
         DetectionResult (spans + entity_counts)
              │
              └── optional DeidApplier(policy) → deidentified_text
```

---

## 2. Prerequisites

```bash
# from the repo root
pip install -e ".[dev]" -c requirements/constraints.txt

# optional NER (GLiNER)
pip install -e ".[ner-runtime]" -c requirements/constraints.txt
# and set REDIBIS_NER_MODEL=/path/to/gliner/weights

# start the web API (loopback recommended)
export USE_LOCAL_STORAGE=true
export SCAN_OUTPUT_DIR=/tmp/redibis_runs
redibis web   # or: uvicorn redibis.webapp.backend:app --host 127.0.0.1 --port 8000
```

Base URL used below: `http://127.0.0.1:8000`.

**Dashboard auth is on by default.** Sign in once and keep the cookie jar —
see [`DASHBOARD_AUTH.md` — Calling the API](DASHBOARD_AUTH.md#calling-the-api-curl).
Every REST example below assumes `$RB_COOKIES` and `$CSRF` are set. The CLI
commands in later sections do not need a session.

---

## 3. REST API

### 3.1 Health — which engines are available?

```bash
curl -s -b "$RB_COOKIES" http://127.0.0.1:8000/api/pii/text/health | jq
```

Example response:

```json
{
  "engines": {
    "regex": { "available": true },
    "phone": { "available": true },
    "ner": { "loaded": false, "type": "gliner", "path": "...", "loadable": false },
    "llm": { "enabled": false, "available": false }
  },
  "ruleset_id": "builtin-default",
  "ruleset_version": "1.0.0",
  "languages": ["ar", "de", "en", "es", "fr", "it", "nl", "pt", "tr"],
  "offset_unit": "unicode_codepoint"
}
```

Use this before enabling `--use-llm` or `engines=ner` in production scripts.

### 3.2 Entities — what can be detected?

```bash
curl -s -b "$RB_COOKIES" "http://127.0.0.1:8000/api/pii/text/entities?language=en" | jq
```

```json
{
  "entities": [
    {
      "entity_type": "EMAIL_ADDRESS",
      "engines": ["ner", "regex"],
      "family": "contact"
    },
    {
      "entity_type": "PHONE_NUMBER",
      "engines": ["ner", "phone", "regex"],
      "family": "telecom"
    },
    {
      "entity_type": "PERSON",
      "engines": ["ner"],
      "family": "identity"
    }
  ],
  "language": "en",
  "offset_unit": "unicode_codepoint",
  "ruleset_id": "builtin-default",
  "ruleset_version": "1.0.0"
}
```

`family` drives UI colors (identity / contact / telecom / financial / government_id / other).
Filter scans with the `entities` request field using these `entity_type` values.

### 3.3 Policies — named de-id policies

```bash
curl -s -b "$RB_COOKIES" http://127.0.0.1:8000/api/pii/text/policies | jq
```

Out of the box you get at least `full-redact`. More named policies can be registered from
pack `masking/` sections as that wiring lands.

### 3.4 Scan — detect spans

**Request**

```bash
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/scan \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{
    "text": "Call John Doe on +20 100 123 4567 or john@acme.com",
    "language": "en",
    "engines": "both",
    "min_score": 0.35,
    "return_text": true,
    "resolve": "priority",
    "use_llm": false,
    "entities": []
  }' | jq
```

**Body fields**

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `text` | string | required | Free text to scan |
| `language` | string | `"en"` | Drives phone region + NER phrases (`ar` → EG region by default) |
| `engines` | string | `"both"` | `"regex"`, `"ner"`, `"phone"`, `"both"`/`"all"`, or comma list |
| `min_score` | float | `0.35` | Drop spans below this confidence after resolution |
| `return_text` | bool | `true` | If `false`, span `text` is empty (offsets only — max privacy) |
| `resolve` | string | `"priority"` | Overlap policy: `priority` \| `longest` \| `all` |
| `use_llm` | bool | `false` | Optional local-LLM proposals (`is_proposal: true`) |
| `entities` | string[] | `[]` | Restrict to these entity types (empty = all) |
| `max_chars` | int | `50000` | Hard input cap → HTTP `413` if exceeded |

**Engine selection notes**

- `"both"` / `"all"` → regex + phone + NER (NER skipped cleanly if no model).
- `"regex"` → regex **and** phone validation (phone gate is cheap and useful).
- `"ner"` → NER only.
- `"phone"` → phone-shaped candidates only.

**Overlap (`resolve`)**

- `priority` — validated phone > validated regex > NER > LLM; non-overlapping winners.
- `longest` — keep the widest match on contested regions.
- `all` — return every candidate (playground “show evidence” mode; overlaps allowed).

**Response (shape)**

```json
{
  "kind": "span",
  "spans": [
    {
      "start": 5,
      "end": 13,
      "entity_type": "PERSON",
      "text": "John Doe",
      "score": 0.86,
      "engine": "ner",
      "recognizer": "gliner:/models/...",
      "validator": "",
      "context_boost": false,
      "is_proposal": false,
      "detected": true
    },
    {
      "start": 37,
      "end": 50,
      "entity_type": "EMAIL_ADDRESS",
      "text": "john@acme.com",
      "score": 0.99,
      "engine": "regex",
      "recognizer": "presidio",
      "validator": "",
      "context_boost": false,
      "is_proposal": false,
      "detected": true
    }
  ],
  "detections": [ "...same as spans..." ],
  "entity_counts": { "PERSON": 1, "EMAIL_ADDRESS": 1 },
  "engines_ran": ["regex", "phone", "ner"],
  "language": "en",
  "char_count": 50,
  "truncated": false,
  "offset_unit": "unicode_codepoint",
  "ruleset_id": "builtin-default",
  "ruleset_version": "1.0.0"
}
```

**Integrity check you should always do client-side (Python):**

```python
for s in result["spans"]:
    assert text[s["start"]:s["end"]] == s["text"]  # when return_text=true
```

**Privacy-friendly call (offsets only):**

```bash
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/scan \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"text":"alice@example.com","return_text":false,"engines":"regex"}' | jq '.spans'
```

**Filter to emails only:**

```bash
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/scan \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{
    "text": "alice@example.com and +1 415 555 2671",
    "engines": "regex",
    "entities": ["EMAIL_ADDRESS", "EMAIL"]
  }' | jq
```

**Oversized input → 413:**

```bash
# max_chars=50 with a longer body
curl -s -o /dev/null -w "%{http_code}\n" -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/scan \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d "{\"text\":\"$(python -c 'print(\"x\"*100)')\",\"max_chars\":50}"
# → 413
```

## 3.4 Scan batch

```bash
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/scan-batch \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"texts":["alice@example.com","bob@acme.com"],"engines":"regex","max_items":50}' | jq
```

Returns `{ "results": [ ... ], "count": N, "offset_unit": "unicode_codepoint" }`.
Over `max_items` → HTTP `413`.

### 3.4a Suggest / save policy

```bash
# Pre-fill a DeidPolicy from a scan (suggest_rule per entity)
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/suggest-policy \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"text":"mail alice@example.com","engines":"regex","policy_id":"suggested"}' | jq

# Persist under masking/policies/{id}.yaml (set REDIBIS_DEID_POLICIES_DIR to override root)
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/policies \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"policy":{"apiVersion":"redibis.io/deid-policy/v1","id":"support-notes","default":{"strategy":"redact"},"overrides":[{"entity_type":"EMAIL_ADDRESS","strategy":"mask","params":{"keep_last":4}}]}}' | jq
```

Note: `resolve: "all"` is rejected on `/deidentify` (overlapping spans); use `priority` or `longest`.


You must supply **either** `policy_id` **or** an inline `policy` document.
No policy → HTTP `400` (fail-closed).

**Named policy (`full-redact`):**

```bash
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/deidentify \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{
    "text": "mail alice@example.com now",
    "engines": "regex",
    "min_score": 0.2,
    "policy_id": "full-redact"
  }' | jq
```

```bash
# Arabic spoken MSISDN (Gateway enables preprocess by default)
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/gateway/scan \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{
    "text": "زيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة (01143215567)",
    "language": "ar",
    "engines": "both"
  }' | jq '.analysers.pii.spans'
```

**Inline policy (general rule + per-entity overrides):**

```bash
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/deidentify \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{
    "text": "Call Jane on 01001234567 or jane@acme.com",
    "language": "ar",
    "engines": "both",
    "policy": {
      "id": "support-notes",
      "version": "1.0.0",
      "locale": "en",
      "default": {
        "entity_type": "*",
        "strategy": "redact",
        "replacement": "[REDACTED]"
      },
      "overrides": [
        {
          "entity_type": "EMAIL_ADDRESS",
          "strategy": "mask",
          "params": { "keep_last": 4 },
          "min_score": 0.5
        },
        {
          "entity_type": "PHONE_NUMBER",
          "strategy": "mask",
          "params": { "keep_last": 4 }
        },
        {
          "entity_type": "PERSON",
          "strategy": "fake",
          "params": { "kind": "name" }
        }
      ],
      "unknown_entity": "redact"
    }
  }' | jq
```

**Strategies**

| Strategy | Effect | Reversible? |
|----------|--------|-------------|
| `redact` | Replace with `[ENTITY]` or `replacement` | no |
| `mask` | Keep some chars (`keep_first` / `keep_last`) | no |
| `hash` | HMAC / truncated hash | no (pseudonymous) |
| `fpe` | Format-preserving encryption | yes, with run key |
| `fake` | Synthetic value (name/email/phone/…) | no |
| `passthrough` | Leave as-is | n/a |

**Precedence:** per-entity override (if `score ≥ min_score`) → `default` → `unknown_entity`.

**Response extras (on top of scan fields):**

```json
{
  "deidentified_text": "mail [EMAIL_ADDRESS] now",
  "applied": [
    {
      "entity_type": "EMAIL_ADDRESS",
      "strategy": "redact",
      "start": 5,
      "end": 22,
      "before_len": 17,
      "after_len": 15,
      "rule_id": "full-redact:*",
      "score": 0.95
    }
  ],
  "policy_id": "full-redact",
  "policy_version": "1.0.0",
  "reversible_spans": 0,
  "run_key_ref": "mask_a1b2c3",
  "original_spans": [ ... ]
}
```

Never expect `master_key`, `seed`, or key bytes in the response — that is an invariant.

### 3.6 Python client sketch

Reuse a session after login. Full cookie + CSRF helper:
[`DASHBOARD_AUTH.md` — Python](DASHBOARD_AUTH.md#python-requests).

```python
import os
import requests

BASE = os.environ.get("RB_BASE", "http://127.0.0.1:8000")
session = requests.Session()
session.get(f"{BASE}/login")
csrf = session.cookies["redibis_csrf"]
session.post(
    f"{BASE}/login",
    json={"username": os.environ.get("RB_USER", "admin"),
          "password": os.environ["RB_PASSWORD"]},
    headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
    timeout=30,
).raise_for_status()
csrf = session.cookies["redibis_csrf"]


def scan_text(text: str, **opts) -> dict:
    r = session.post(
        f"{BASE}/api/pii/text/scan",
        json={"text": text, **opts},
        headers={"X-CSRF-Token": csrf},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def deidentify(text: str, policy_id: str = "full-redact", **opts) -> dict:
    r = session.post(
        f"{BASE}/api/pii/text/deidentify",
        json={"text": text, "policy_id": policy_id, **opts},
        headers={"X-CSRF-Token": csrf},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()

result = scan_text("ping alice@example.com", engines="regex", min_score=0.2)
for s in result["spans"]:
    print(s["entity_type"], s["start"], s["end"], s["text"], s["engine"])

clean = deidentify("ping alice@example.com", engines="regex", min_score=0.2)
print(clean["deidentified_text"])
```

### 3.7 Library API (same engine as REST/CLI)

```python
from redibis.pii import TextPIIScan, TextScanConfig, DeidPolicy

result = TextPIIScan(TextScanConfig(engines="regex", language="en")).scan(
    "Contact alice@example.com"
)
for s in result.spans:
    print(s.entity_type, s.start, s.end, s.text)

out = TextPIIScan().scan_and_deidentify(
    "Contact alice@example.com",
    DeidPolicy.redact_all(),
)
print(out.deidentified_text)
```

---

## 4. CLI

### 4.1 `redibis pii text` — detect spans

**Inline string**

```bash
redibis pii text "Call me at alice@example.com"
```

Default table output:

```
ENTITY             START   END  SCORE ENGINE   TEXT
EMAIL_ADDRESS         11    28   0.99 regex    alice@example.com

1 span(s)  engines=['regex', 'phone']
```

**JSON**

```bash
redibis pii text "Call me at alice@example.com" --json
```

**From a file**

```bash
redibis pii text --file notes.txt --json
```

**From stdin**

```bash
cat notes.txt | redibis pii text -
# or
redibis pii text --file - < notes.txt
```

**Redact in place (print text with `[ENTITY_TYPE]` replacements)**

```bash
redibis pii text "mail alice@example.com end" --redact
# → mail [EMAIL_ADDRESS] end
```

**Useful flags**

| Flag | Meaning |
|------|---------|
| `--language en\|ar\|…` | Phone region / locale hint |
| `--engines both\|regex\|ner\|phone` | Engine set (same semantics as API) |
| `--min-score 0.35` | Confidence floor |
| `--resolve priority\|longest\|all` | Overlap policy |
| `--entities EMAIL_ADDRESS,PHONE_NUMBER` | Comma-separated allowlist |
| `--use-llm` | Local LLM proposals (requires `pii.llm.enabled`) |
| `--json` | Emit `DetectionResult` JSON |
| `--redact` | Print redacted source text |
| `--no-text` | JSON with empty `text` fields (offsets only) |
| `--config path/to/redibis.yaml` | Load PII/NER/LLM settings |

**Examples**

```bash
# Regex-only, Arabic phone region, JSON without matched substrings
redibis pii text --file tickets.txt \
  --engines regex --language ar --json --no-text

# Only phones, show all overlapping evidence
redibis pii text "MSISDN 01001234567 in CGI 12345" \
  --entities PHONE_NUMBER --resolve all --json
```

### 4.2 `redibis pii deid` — scan + apply policy

You must choose a policy source:

1. `--policy <id>` — named (e.g. `full-redact`)
2. `--policy-file <yaml>` — YAML `DeidPolicy` document
3. `--default <strategy>` plus optional `--set ENTITY=strategy` — ad-hoc

**Full redact**

```bash
redibis pii deid "mail alice@example.com now" --policy full-redact
# → mail [EMAIL_ADDRESS] now
```

**Write to a file**

```bash
redibis pii deid --file notes.txt --policy full-redact -o notes_safe.txt
```

**Ad-hoc default + overrides**

```bash
redibis pii deid "Jane / jane@acme.com / 01001234567" \
  --default redact \
  --set EMAIL_ADDRESS=mask \
  --set PHONE_NUMBER=mask \
  --set PERSON=fake
```

**Policy file** (`policies/support-notes.yaml`):

```yaml
id: support-notes
version: "1.0.0"
locale: en
default:
  strategy: redact
  replacement: "[REDACTED]"
overrides:
  - entity_type: EMAIL_ADDRESS
    strategy: mask
    params: { keep_last: 4 }
  - entity_type: PHONE_NUMBER
    strategy: mask
    params: { keep_last: 4 }
  - entity_type: NATIONAL_ID
    strategy: fpe
    params: { mode: ff3, alphabet: digits, key_ref: k1 }
    min_score: 0.6
unknown_entity: redact
```

```bash
redibis pii deid --file notes.txt --policy-file policies/support-notes.yaml -o safe.txt
```

**Audit JSON on stderr** (de-identified text still on stdout / `-o`):

```bash
redibis pii deid "alice@example.com" --policy full-redact --json 2> audit.json
```

**Fail-closed (no policy) → exit 2**

```bash
redibis pii deid "alice@example.com"
# pii deid: require --policy, --policy-file, or --default STRATEGY
```

### 4.3 `redibis pii eval` — batch accuracy against expected spans

Full reference and worked examples:
[`TEXT_PII_EVAL.md`](TEXT_PII_EVAL.md),
[`tutorials/TEXT_PII_EVAL_TUTORIAL.md`](tutorials/TEXT_PII_EVAL_TUTORIAL.md),
[`cli/pii-eval.md`](cli/pii-eval.md).

Build a dataset in **Gateway → Evaluations** (paste text, highlight spans,
assign entity types, download JSON) or write the file by hand. Offsets are
Unicode code points.

```json
{
  "kind": "redibis.text_span_eval_dataset",
  "schema_version": "1.0",
  "offset_unit": "unicode_codepoint",
  "cases": [
    {
      "id": "email-en",
      "text": "Contact alice@example.com please",
      "language": "en",
      "expected_spans": [
        {"start": 8, "end": 25, "entity_type": "EMAIL_ADDRESS"}
      ]
    }
  ]
}
```

```bash
redibis pii eval --dataset eval.json --engines regex -o report.json
redibis pii eval --dataset eval.json --min-exact-f1 0.9
redibis pii eval --dataset ./cases --recursive --out-dir ./eval-out --html report.html
```

`--min-exact-f1` exits 1 when exact (strict) micro F1 is below the threshold (CI gate).
`--gate-file` names the failing entity, tier and metric. A folder continues after
invalid files unless `--fail-fast` is set; report paths are relative. `--out-dir`
writes `evaluation-report.json` plus escaped standalone HTML. `--rules-defaults`
or `--rules FILE` pins the Text Gateway overlay so two machines score the same.
`--normalization v1` selects the value-tier profile. Overlap F1 (default IoU 0.5)
is secondary. `--use-llm` / `--llm-provider` / `--llm-model` match `pii text`
and still go through `guarded_model_call`.

Author corpora at value level (`tests/data/text_pii/corpora/*.yaml`) and compile
offsets with `redibis pii eval-build`. The builder fails closed if a value is
not found verbatim in the case text.

The streamed UI/API (`POST /api/gateway/evaluations/run/stream`) emits NDJSON
progress. Closing the connection cancels later cases; an in-flight model call
is bounded by the provider timeout.

The report includes per-case TP/FP/FN plus `exact` and `overlap` micro scores.
LLM proposals are listed separately and do not count as findings. Table-column
evaluation is a separate command: see [`TABLE_COLUMN_EVAL.md`](TABLE_COLUMN_EVAL.md).

---

## 5. End-to-end recipes

### 5.1 Ticket triage: find emails and phones, emit JSON

```bash
redibis pii text --file tickets.txt \
  --engines both --entities EMAIL_ADDRESS,PHONE_NUMBER,EMAIL,PHONE \
  --json > findings.json
```

### 5.2 Support export: redact everything before sharing

```bash
redibis pii deid --file support_thread.txt --policy full-redact -o support_thread_safe.txt
```

### 5.3 API pipeline: scan → review → de-id with custom policy

```bash
# 1) discover
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/scan \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d @scan_request.json > scan_result.json

# 2) after human review of spans, apply policy
curl -s -b "$RB_COOKIES" -X POST http://127.0.0.1:8000/api/pii/text/deidentify \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d @deid_request.json > deid_result.json
```

### 5.4 Batch files via shell

```bash
mkdir -p safe
for f in raw/*.txt; do
  redibis pii deid --file "$f" --policy full-redact -o "safe/$(basename "$f")"
done
```

---

## 6. Errors & troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| HTTP `401` `authentication required` | No session cookie | Sign in first — [`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl) |
| HTTP `403` `CSRF header required (X-CSRF-Token)` | Missing `X-CSRF-Token` on POST | Send the `redibis_csrf` cookie value as the header |
| HTTP `413` / CLI refuses long input | Over `max_chars` (default 50k) | Raise `max_chars` or chunk the text |
| HTTP `400` on `/deidentify` | No `policy_id` / `policy` | Pass a policy; fail-closed by design |
| Empty `spans` | Score floor, wrong engine, or no pattern match | Lower `--min-score`, try `--engines regex`, check `/health` |
| No PERSON / ADDRESS | NER model not loaded | Install `[ner-runtime]`, set `REDIBIS_NER_MODEL`, confirm `/health` |
| `--use-llm` does nothing | `pii.llm.enabled` false / no provider | Enable in YAML; confirm `/health` → `llm.available` |
| Offsets wrong in browser | Using JS UTF-16 indexes | Use `Array.from(text)` code-point walk (playground already does) |
| Phone false positives on CGI/LAC | Network IDs | Phone gate + telecom rules drop network identifiers; prefer validated `engine=phone` |

**Logging:** request bodies are **not** logged. Info logs only record char count, entity
counts, engines, language, and latency.

**Auth:** these REST routes use the dashboard session + CSRF gate (default **on**).
The CLI does not. Do not set `auth.enabled: false` except on a local loopback
process. See [`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md).

---

## 7. Quick reference card

```bash
# After login (DASHBOARD_AUTH.md). CLI commands below skip the dashboard session.

# Health / catalogue
curl -s -b "$RB_COOKIES" localhost:8000/api/pii/text/health | jq
curl -s -b "$RB_COOKIES" localhost:8000/api/pii/text/entities | jq
curl -s -b "$RB_COOKIES" localhost:8000/api/pii/text/policies | jq

# Scan
curl -s -b "$RB_COOKIES" -X POST localhost:8000/api/pii/text/scan \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"text":"alice@example.com","engines":"regex"}' | jq

# De-id
curl -s -b "$RB_COOKIES" -X POST localhost:8000/api/pii/text/deidentify \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"text":"alice@example.com","policy_id":"full-redact","engines":"regex"}' | jq

# CLI
redibis pii text "alice@example.com" --json
redibis pii text --file notes.txt --redact
redibis pii deid --file notes.txt --policy full-redact -o safe.txt
redibis pii deid "..." --default redact --set EMAIL_ADDRESS=mask
```

UI playground (same backend): Results → **PII Text Playground**, or open the link from a
completed scan’s artifacts panel.
