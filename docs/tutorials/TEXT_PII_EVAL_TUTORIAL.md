# Tutorial — free-text PII evaluation

Worked examples for scoring Text Gateway detections against labeled spans.
Feature reference: [`TEXT_PII_EVAL.md`](../TEXT_PII_EVAL.md). CLI flags:
[`cli/pii-eval.md`](../cli/pii-eval.md).

| Page | What you do | Time |
|---|---|---|
| [A. Score the sample corpus](#a-score-the-sample-corpus) | `eval-build` + `eval` + HTML | 5 min |
| [B. Trailing punctuation](#b-trailing-punctuation--the-near-miss) | why strict F1 is 0 and value F1 is 1 | 10 min |
| [C. Evaluation Builder UI](#c-evaluation-builder-ui) | paste, classify, read the overlay | 10 min |
| [D. Gateway API](#d-gateway-api) | curl run / compare / corpus-patch | 10 min |
| [E. Gates in CI](#e-gates-in-ci) | a failure that names entity + tier + metric | 5 min |

Install once (`pip install -e ".[dev,web]"` is enough for regex-only eval).
You do **not** need NER or an LLM for these pages.

---

## A. Score the sample corpus

The shipped sample lives next to the tests:

```
tests/data/text_pii/
  corpora/sample.yaml      # authored values, no offsets
  datasets/sample.json     # committed offsets (do not edit by hand)
  gates/all.yaml           # demo thresholds
```

Rebuild offsets, then score with **pinned** shipped rules:

```bash
redibis pii eval-build \
  --corpus tests/data/text_pii/corpora \
  --out tests/data/text_pii/datasets

redibis pii eval \
  --dataset tests/data/text_pii/datasets \
  --recursive \
  --rules-defaults \
  --normalization v1 \
  --tier strict,value,overlap,type \
  --gate-file tests/data/text_pii/gates/all.yaml \
  --engines regex \
  --out-dir reports/eval
```

Same thing: `make eval`.

You should see:

```
pii eval-build: wrote 1 dataset(s) to tests/data/text_pii/datasets
  tests/data/text_pii/datasets/sample.json
```

and under `reports/eval/`:

| File | Open |
|---|---|
| `evaluation-report.json` | four tiers, `match_classes`, `provenance` |
| `evaluation-report.html` | offline; provenance header + KPI tiles + overlays |

Check the JSON quickly:

```bash
python - <<'PY'
import json
r = json.load(open("reports/eval/evaluation-report.json"))
p = r["provenance"]
print("run", p["run_uuid"])
print("rules", p["rules_checksum"][:19], "…")
print("norm", r["normalization_profile"])
print("strict F1", r["strict"]["micro"]["f1"], "value F1", r["value"]["micro"]["f1"])
print("classes", r["class_distribution"])
PY
```

`--rules-defaults` is load-bearing. Without it, a leftover
`global_settings.pii_text_rules` overlay on one machine changes the numbers
and nothing in an unpinned report would tell you why.

Corpus YAML and committed JSON must stay in sync. CI runs
`tests/test_pii_eval_corpus_sync.py` (also `make eval-check`). If you edit
`corpora/sample.yaml`, rebuild and commit `datasets/sample.json` in the same PR.

### Authoring a new case

Add a YAML row with the **substring as it appears in `text`**. The builder
does not fuzzy-match:

```yaml
  - id: ticket-email
    text: "please write to ops@example.com today"
    language: en
    tags: [email, tickets]
    expected:
      - id: s1
        entity_type: EMAIL_ADDRESS
        value: ops@example.com
        grade: strict
```

A typo fails the build with the case id (exit 1), instead of scoring as a
model miss:

```text
pii eval-build: ticket-email: value 'ops@exmaple.com' not found verbatim in text (value='ops@exmaple.com')
```

---

## B. Trailing punctuation — the near-miss

This is the failure mode the feature exists to make legible.

**Text:** `Call 01522345678.`  
**Expected:** characters `[5, 16)` → `01522345678`  
**Predicted (over-capture):** `[5, 17)` → `01522345678.`

Save as `reports/eval/trailing.json`:

```json
{
  "kind": "redibis.text_span_eval_dataset",
  "schema_version": "1.2",
  "offset_unit": "unicode_codepoint",
  "id": "tutorial.trailing",
  "normalization_profile": "v1",
  "cases": [
    {
      "id": "phone-dot",
      "text": "Call 01522345678.",
      "language": "en",
      "tags": ["phone", "trailing_punct"],
      "expected_spans": [
        {
          "id": "s1",
          "start": 5,
          "end": 16,
          "entity_type": "PHONE_NUMBER",
          "canonical": "01522345678",
          "grade": "strict"
        }
      ]
    }
  ]
}
```

Confirm the slice yourself:

```bash
python - <<'PY'
t = "Call 01522345678."
print(repr(t[5:16]))  # '01522345678'
print(repr(t[5:17]))  # '01522345678.'
PY
```

You can score **without** running the detector by using the Python helpers
(same code the CLI uses after a scan):

```bash
python - <<'PY'
from redibis.pii.eval import evaluate_case
case = {
    "id": "phone-dot",
    "text": "Call 01522345678.",
    "expected_spans": [{"id": "s1", "start": 5, "end": 16, "entity_type": "PHONE_NUMBER"}],
}
# Pretend the engine returned the number plus the sentence period.
scored = evaluate_case(case, [{"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"}])
print("strict", scored["strict"])
print("value ", scored["value"])
row = next(r for r in scored["match_classes"] if r["class"] != "spurious")
print(row["class"], "coverage", row["coverage"], "Δ", row["delta_label"], "extra_right", repr(row["extra_right"]))
PY
```

Expected print:

```
strict {'tp': 0, 'fp': 1, 'fn': 1, ... 'f1': 0.0 ...}
value  {'tp': 1, 'fp': 0, 'fn': 0, ... 'f1': 1.0 ...}
superset coverage 1.0 Δ +1 '.' extra_right '.'
```

Reading that:

1. **strict F1 = 0** — the extra `.` would be masked; that is a real boundary miss.
2. **value F1 = 1** — after profile `v1` (`strip_edge_punct`) the strings match.
3. **class = `superset`** — not `missed` and not `spurious`.

### Wrong label, same offsets

```bash
python - <<'PY'
from redibis.pii.eval import evaluate_case
case = {
    "id": "t",
    "text": "12345678901234",
    "expected_spans": [{"id": "s1", "start": 0, "end": 14, "entity_type": "VOUCHER"}],
}
scored = evaluate_case(case, [{"start": 0, "end": 14, "entity_type": "EG_NATIONAL_ID"}])
print([r["class"] for r in scored["match_classes"]])
print("type accuracy", scored["type"]["accuracy"])
PY
```

Prints `['type_mismatch']` and type accuracy `0.0`. That is a labeling bug,
not a detection miss.

### Forbidden span (guard)

```bash
python - <<'PY'
from redibis.pii.eval import evaluate_case
text = "عمارة 4 شقة 12"
case = {
    "id": "g",
    "text": text,
    "expected_spans": [{"id": "s1", "start": 0, "end": len(text), "entity_type": "LOCATION", "grade": "advisory", "defect": "demo"}],
    "forbidden_spans": [
        {"start": text.find("4"), "end": text.find("4") + 1, "entity_type": "PHONE_NUMBER", "reason": "building number"}
    ],
}
scored = evaluate_case(case, [{"start": text.find("4"), "end": text.find("4") + 1, "entity_type": "PHONE_NUMBER"}])
print("guards", scored["guard_violations"])
print([r["class"] for r in scored["match_classes"]])
print("strict fn (advisory excluded)", scored["strict"]["fn"])
PY
```

Advisory expected spans never affect pass/fail. The `4` predicted as a phone
is a **guard_violation** (`reason`: building number).

---

## C. Evaluation Builder UI

1. Start the dashboard (`./scripts/webapp.sh --start --venv`) and sign in.
2. Open **http://127.0.0.1:8000/gateway/evaluations** (also **◈ Evaluations** in the nav).
3. Paste:

   ```
   Call 01522345678.
   ```

4. Select `01522345678` (not the period). Set **Expected type** to
   `PHONE_NUMBER`. Click **Classify selection**.
5. Set **Engines** to **Regex only**. Leave **Use LLM refiner** unchecked.
6. Click **Run evaluation**.

What to look at, top to bottom:

| Widget | What it should show |
|---|---|
| Provenance line | `run … · redibis … · rules sha256:… · norm v1 · engines …` |
| Accuracy tiles | **Strict F1** and **Value F1** side by side |
| Match classes | counts for `exact` / `superset` / `missed` / … |
| Text canvas | expected underline + predicted tint; extra chars in **yellow** if the engine over-captured |
| Span table | Expected, Predicted, Class, Coverage, Char prec., Δ, Type, Engine |
| Per entity | value F1 / recall / precision for `PHONE_NUMBER` |

Click a span-table row. The **why** line lists class, coverage, engine,
recognizer, score. That enables the draft actions
(`+ exclude term`, `+ context cue`, `+ number rule`, **accept as expected**,
**mark advisory**). Those download a corpus patch / draft overlay — they do
not `PUT` production rules.

**Near-misses only** = coverage ≥ 90% and class ≠ `exact`. Use it after a
rule change to review “almost right” rows without wading through exact hits.

**Compare runs:** copy two `run_uuid`s from provenance (or
`GET /api/gateway/evaluations/runs`) into the Compare card. The API returns
cases whose class changed (`exact → subset`, …).

Datasets stay in the **browser tab**. Use **Download dataset** / **Import JSON**
to keep them. The page never writes `localStorage`.

If **Classify selection** seems to do nothing, check that the substring is
still selected in the textarea (the button no longer requires the textarea to
keep focus). Changing the pasted text clears expected spans for that case.

---

## D. Gateway API

Sign in once as in
[`DASHBOARD_AUTH.md` — Calling the API](../DASHBOARD_AUTH.md#calling-the-api-curl).
Reuse `$RB_COOKIES` and `$CSRF`. Explorers can POST `/api/gateway/*`
(compute-only; CSRF still required).

### Run

```bash
curl -s -b "$RB_COOKIES" -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -X POST "$RB_BASE/api/gateway/evaluations/run" \
  --data-binary @- <<'JSON' | python -m json.tool | head -40
{
  "dataset": {
    "kind": "redibis.text_span_eval_dataset",
    "schema_version": "1.2",
    "offset_unit": "unicode_codepoint",
    "cases": [
      {
        "id": "email-en",
        "text": "Contact alice@example.com please",
        "language": "en",
        "expected_spans": [
          {"id": "s1", "start": 8, "end": 25, "entity_type": "EMAIL_ADDRESS"}
        ]
      }
    ]
  },
  "engines": "regex",
  "normalization": "v1",
  "label": "tutorial-d"
}
JSON
```

The body is the same report the CLI writes: `exact` / `strict` / `value` /
`overlap` / `type`, `match_classes`, `provenance.run_uuid`.

Progress (NDJSON) is `POST /api/gateway/evaluations/run/stream`. Closing the
connection cancels later cases; a model call already in flight is bounded by
the provider timeout.

### Fetch and compare

```bash
UID=$(python -c "import json,sys; print(json.load(sys.stdin)['provenance']['run_uuid'])" < report.json)

curl -s -b "$RB_COOKIES" "$RB_BASE/api/gateway/evaluations/runs"
curl -s -b "$RB_COOKIES" "$RB_BASE/api/gateway/evaluations/runs/$UID"
curl -s -b "$RB_COOKIES" "$RB_BASE/api/gateway/evaluations/runs/$UID/cases/email-en"
curl -s -b "$RB_COOKIES" "$RB_BASE/api/gateway/evaluations/compare?a=$UID&b=$UID"
```

Compare two different `run_uuid`s after a rule change. The payload’s
`changes[]` list is the review artifact: `{case_id, expected_id, from, to}`.

### Corpus patch (draft only)

```bash
curl -s -b "$RB_COOKIES" -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -X POST "$RB_BASE/api/gateway/evaluations/corpus-patch" \
  --data-binary @- <<'JSON'
{
  "dataset": {
    "kind": "redibis.text_span_eval_dataset",
    "schema_version": "1.2",
    "cases": [
      {
        "id": "c1",
        "text": "Call 01522345678.",
        "expected_spans": [
          {"id": "s1", "start": 5, "end": 16, "entity_type": "PHONE_NUMBER"}
        ]
      }
    ]
  },
  "actions": [
    {
      "action": "accept_as_expected",
      "case_id": "c1",
      "expected_id": "s1",
      "span": {"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"}
    },
    {"action": "exclude_term", "term": "agent"}
  ]
}
JSON
```

`accept_as_expected` rewrites the dataset span to `[5, 17)`. `exclude_term`
shows up under `draft_rules.exclude_terms`. Neither call updates
`PUT /api/pii/text/rules`.

---

## E. Gates in CI

Write a gate file that will fail on purpose, then read the exit message:

```bash
cat > /tmp/eval-gates-strict.yaml <<'YAML'
micro:
  value_f1: 0.99
  strict_f1: 0.99
by_entity:
  EMAIL_ADDRESS:
    value_recall: 0.99
guard_violations_max: 0
advisory: report_only
YAML

redibis pii eval \
  --dataset tests/data/text_pii/datasets/sample.json \
  --rules-defaults \
  --engines regex \
  --gate-file /tmp/eval-gates-strict.yaml \
  -o /tmp/eval-gated.json
echo "exit=$?"
```

On failure stderr looks like:

```
pii eval: gate failure: * value f1 0.5 < 0.99; EMAIL_ADDRESS value recall 1.0 < 0.99
```

(Exact numbers depend on the current engine.) The important part is the
**entity · tier · metric** triple, not a single micro F1.

`--min-exact-f1 0.9` is the older one-number gate on **strict** micro F1; keep
it for existing jobs and add `--gate-file` when you need per-entity value
recall.

Pin rules in CI the same way every time:

```bash
redibis pii eval \
  --dataset tests/data/text_pii/datasets \
  --recursive \
  --rules-defaults \
  --normalization v1 \
  --gate-file tests/data/text_pii/gates/all.yaml \
  --engines regex \
  --out-dir reports/eval
```

Optional pre-commit hook (local, `language: system`): `.pre-commit-config.yaml`
runs `tests/test_pii_eval_corpus_sync.py` when corpus/dataset files change.

---

## Related

| Doc | Role |
|---|---|
| [`TEXT_PII_EVAL.md`](../TEXT_PII_EVAL.md) | Scoring, schema 1.2, gates, API table |
| [`cli/pii-eval.md`](../cli/pii-eval.md) | Flag cheat sheet |
| [`TEXT_PII_SCAN.md`](../TEXT_PII_SCAN.md) | Scan / de-id, not scoring |
| [`TEXT_PII_API_CLI_TUTORIAL.md`](../TEXT_PII_API_CLI_TUTORIAL.md) | `pii text` / `pii deid` |
| [`TABLE_COLUMN_EVAL.md`](../TABLE_COLUMN_EVAL.md) | Structured column eval (`redibis eval`) |
| [`DASHBOARD_AUTH.md`](../DASHBOARD_AUTH.md) | Cookie + CSRF for curl |
