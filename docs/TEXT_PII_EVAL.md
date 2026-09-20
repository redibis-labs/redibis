# Free-text PII evaluation

Score Text Gateway detections against labeled spans. This is **not** table-column
evaluation (`redibis eval` / Data → Evaluation) and it **does not** write
contracts.

**Hands-on walkthroughs:** [`tutorials/TEXT_PII_EVAL_TUTORIAL.md`](tutorials/TEXT_PII_EVAL_TUTORIAL.md)  
**CLI cheat sheet:** [`cli/pii-eval.md`](cli/pii-eval.md)  
**Scan / de-id (find PII, don't score it):** [`TEXT_PII_SCAN.md`](TEXT_PII_SCAN.md)

```
authored YAML (values)  ──►  redibis pii eval-build  ──►  dataset JSON (offsets)
                                                                  │
                         Text Gateway scan  ──►  four scoring tiers + per-span class
                                                                  │
                              JSON / HTML / /gateway/evaluations
```

Offsets are **Unicode code points**. In Python, `text[start:end]` is the span.
In JavaScript, walk with `Array.from(text)`, not UTF-16 indexes.

---

## Why this exists

Exact offset matching scores **0** when a prediction captures the whole expected
value plus a trailing `.` or a leading space. That is not a detection failure,
and de-identification still cares about the extra characters (they get masked).

The report therefore keeps **strict** matching for boundary regression, and adds
a **value** tier plus a **per-span class** that names the near-miss:

| What you asked | What you get |
|---|---|
| Did we capture the PII? | `value` F1 |
| Are the boundaries exact? | `strict` / `exact` F1 (same numbers) |
| Did we find it at all? | `overlap` F1 (IoU) |
| Did we label it correctly? | `type` accuracy |
| *How* did this one span differ? | `match_classes[]` (`superset`, `subset`, …) |

A trailing-dot prediction on `"Call 01522345678."`:

| | Offsets | Class | coverage | char_precision | Δ |
|---|---|---|---|---|---|
| expected | `[5, 16)` `01522345678` | | | | |
| predicted | `[5, 17)` `01522345678.` | **superset** | 100% | ~92% | `+1 '.'` |

- **strict** still records FN + FP (the extra character would over-mask).
- **value** records TP (normalized strings match).
- The class is **never** `missed` + `spurious`.

---

## Scoring tiers

All four appear with `micro` and `by_entity`. `strict` is an alias of `exact`
so `--min-exact-f1` and existing JSON consumers keep working.

| Tier | Match rule | Use |
|---|---|---|
| `strict` / `exact` | identical offsets **and** identical `entity_type` | de-id / regression |
| `value` | normalized strings identical **and** type matches | day-to-day “did we get the PII?” |
| `overlap` | IoU ≥ τ (default 0.5) **and** type matches | “did we find it at all?” |
| `type` | **accuracy only** of `entity_type` over spans paired at value/IoU, ignoring type | wrong label vs miss. Precision/recall/f1 are not emitted |

`value` F1 is ≥ `strict` F1 when the profile is stable (every exact hit is a
value hit). LLM-only spans (`is_proposal: true`) are listed separately and do
not count as findings.

Partial classes carry `value_equal` (normalized strings match) and
`canonical_match` (`yes` / `no` / `unknown` / `n/a`). A trailing-dot `superset`
has `value_equal: true`; extra content that changes the digits does not.
The UI badge is driven from `value_equal`, not from the class name alone.

Guard-violating predictions are **double-counted**: `class=guard_violation` and
a false positive on every scored tier.

Advisory golds are excluded from tp/fn. Predictions that overlap an advisory
gold are also dropped from FP (`advisory_predictions`).

### Per-span classes

Every expected span gets **exactly one** class. Leftover predictions are
`spurious`. Predictions that land on `forbidden_spans` are `guard_violation`.

| Class | Meaning | Evidence on the row |
|---|---|---|
| `exact` | identical offsets and type | — |
| `equivalent` | normalized values match, offsets differ | `extra_left` / `extra_right` |
| `superset` | prediction contains all of expected plus extra | extras, `char_precision` |
| `subset` | prediction inside expected (truncated) | `missing_left` / `missing_right`, `coverage` |
| `overlap_partial` | overlap both ways | coverage, char precision, IoU |
| `type_mismatch` | boundary matched, wrong type | `expected_type`, `got_type` |
| `split` | several predictions cover one expected | `predicted_ids` |
| `merged` | one prediction covers several expected | `expected_ids` |
| `missed` | nothing predicted | — |
| `spurious` | prediction with no expected counterpart | — |
| `guard_violation` | prediction overlaps a forbidden span | `reason` |

Two ratios on every partial class:

```
coverage       = |expected ∩ predicted| / |expected|
char_precision = |expected ∩ predicted| / |predicted|
```

Example row:

```json
{
  "class": "superset",
  "coverage": 1.0,
  "char_precision": 0.916667,
  "extra_right": ".",
  "delta_label": "+1 '.'",
  "expected_type": "PHONE_NUMBER",
  "got_type": "PHONE_NUMBER"
}
```

A `VOUCHER` span labeled `EG_NATIONAL_ID` is `type_mismatch` (coverage 100%),
not miss + FP.

---

## Normalization profile `v1`

Value-tier matching is only reproducible if normalization is named. The profile
id is stamped on every report (`normalization_profile`). A value F1 without a
profile id is not a number anyone can reproduce.

Shipped as `redibis/pii/eval/profiles/v1.yaml` and
`configs/eval_normalization.v1.yaml`:

```yaml
profile: v1
all:            [trim, collapse_whitespace, strip_edge_punct]
digit_family:   [fold_arabic_indic, digits_only]   # PHONE_NUMBER, EG_NATIONAL_ID, …
arabic_text:    [fold_ar, strip_zero_width, unify_yeh]
email:          [casefold, strip_spaces]
mac_ip:         [casefold, strip_spaces]
never_normalize_across_families: true
```

`fold_ar` already folds alef, teh marbuta, diacritics, and tatweel. Independent
steps (`unify_alef`, `strip_diacritics`, …) exist so a v2 profile can select
one without silently applying the rest.

Helpers are reused (`fold_ar`, `fold_indic_digits`, `token_normalize`) — there
is no second Arabic normalizer.

| Surface | After `v1` |
|---|---|
| `"0100 2030 450"` vs `"01002030450"` | equal (`digits_only`) |
| `"٠١٠٠٢٠٣٠٤٥٠"` vs `"01002030450"` | equal (Indic fold + digits) |
| `"01522345678."` vs `"01522345678"` | equal (`strip_edge_punct`) |
| spoken gold + identical offsets, no pred `canonical` | **surface vs surface** (not canonical vs words). `canonical_match: unknown` |
| spoken gold + pred `canonical` | canonical ↔ canonical. `canonical_match: yes/no` |

Pass `--normalization v1` (the default). A spoken gold with `canonical` no
longer destroys `value` F1 when the engine matches the spoken offsets.

`--tier` selects which blocks are **computed and emitted**. `--tier strict`
omits `value` / `overlap` / `type`. Unknown names fail before scoring.

---

## Dataset schema 1.2

Kind: `redibis.text_span_eval_dataset`. `validate_dataset` still accepts **1.0**.
Reports are emitted as **1.2**.

Author at **value** level (no offsets). The builder locates each value
verbatim in `text` and fails closed on typos.

```yaml
# tests/data/text_pii/corpora/sample.yaml (excerpt)
kind: redibis.text_span_eval_corpus
schema_version: "1.2"
id: sample.free_text_pii
normalization_profile: v1
cases:
  - id: phone-trailing-punct
    text: "Call 01522345678."
    language: en
    tags: [phone, trailing_punct]
    expected:
      - id: s1
        entity_type: PHONE_NUMBER
        value: "01522345678"
        canonical: "01522345678"
        grade: strict
```

After `redibis pii eval-build` the committed dataset has offsets:

```json
{
  "id": "s1",
  "start": 5,
  "end": 16,
  "entity_type": "PHONE_NUMBER",
  "grade": "strict",
  "canonical": "01522345678"
}
```

| Field | Role |
|---|---|
| `grade: strict` | counted in gates |
| `grade: advisory` | reported with `defect`, **never** gates |
| `grade: structure_only` | detection + type scored; excluded from strict only (synthetic IDs that fail Luhn/NID by construction) |
| `forbidden_spans` | must **not** find; default gate is zero violations |
| `id` on each expected span | UI + `split` / `merged` references |
| `tags` | UI filters + `by_tag` metrics. Case tags inherit onto spans only when the case has a **single** expected span; otherwise set `tags` on the span |

Nine forbidden entries on the sample address case (`address-guards`) encode
“building / apartment / street numbers are not phones, OTPs, or IDs”.

---

## Pinning (reproducibility)

Without `--rules` / `--rules-defaults`, Text Gateway may merge persisted
`global_settings.pii_text_rules`, so two machines score the same corpus
differently. Pin every CI run:

| Flag | Effect |
|---|---|
| `--rules FILE` | overlay from YAML (use `replace_defaults: true` for a complete set) |
| `--rules-defaults` | shipped defaults only; skip persisted overlay |
| `--draft-rules FILE` | merged on top; **never** writes production rules |
| `--pack` / `--pack-stack` | score against a specific pack path |
| `--run-uuid` / `--label` | name the run in the in-memory registry |

Every report names:

`run_uuid` · `redibis_version` · pack stack · `rules_checksum` (`sha256:…`) ·
`normalization_profile` · engines that ran / were unavailable.

---

## Gates and baseline

`--gate-file` is pure post-processing of `by_entity`. Exit 1 names the failing
**entity, tier, and metric**. Unknown metric keys (`value_F1`, `strict_recal`)
and `class_budgets` keys that do not end in `_max` raise at load time (fail
closed). A gate that names an entity with no spans in the corpus raises
(`gate names PASSPORT; corpus has no PASSPORT spans`).

`class_budgets` ratios use **matched** classes only (they exclude
`missed` / `spurious` / `guard_violation` from the denominator).

```yaml
# tests/data/text_pii/gates/all.yaml — demo thresholds (loose)
micro:
  value_f1: 0.50
  strict_f1: 0.40
by_entity:
  EMAIL_ADDRESS:
    value_recall: 0.50
guard_violations_max: 0
class_budgets:
  superset_max: 0.80
  subset_max: 0.50
advisory: report_only
regression:
  max_drop: 0.50
```

A production-shaped file (not the demo) looks like:

```yaml
micro:
  value_f1: 0.90          # day-to-day
  strict_f1: 0.80         # boundary regression, deliberately lower
by_entity:
  PHONE_NUMBER:   {value_recall: 0.95, value_precision: 0.98}
  LOCATION:       {strict_f1: 0.75}     # composed addresses: boundary IS the deliverable
guard_violations_max: 0
class_budgets:
  superset_max: 0.15
  subset_max: 0.05
advisory: report_only
```

`--baseline reports/baseline.json` plus `regression.max_drop` fails when a
tier F1 drops more than the ceiling. The same ceiling is applied per entity on
the value (or strict) `by_entity` table. Override per entity with
`regression.by_entity.PHONE_NUMBER.max_drop`.

`--min-exact-f1` still gates **strict/exact** micro F1.

---

## Surfaces

| Surface | What it is |
|---|---|
| `redibis pii eval-build` | corpus YAML → dataset JSON |
| `redibis pii eval` | scan + score; JSON and/or standalone HTML |
| `make eval` / `make eval-check` | sample corpus rebuild + score (pinned `--rules`) / corpus-sync test |
| `/gateway/evaluations` | interactive builder (explorer role) |
| `/gateway/usecases` | versioned use-case assets (mark, save, download, run) |
| `/gateway/runs/{run_uuid}` | HTML report for a persisted evaluation run |
| `POST /api/gateway/evaluations/run` | same report as CLI |
| `GET /api/gateway/evaluations/compare?a=&b=` | per-case class-change diff |
| `POST /api/gateway/evaluations/corpus-patch` | accept-as-expected / draft rules (no production write) |

HTML and the web UI read the **same report JSON**, so they cannot diverge on
tiers or classes. HTML is offline (no CDN, no `<script>`).

### UI overlay

The case text is drawn once:

- expected: underline
- predicted: tint by class (green exact/equivalent, amber near-miss, red mismatch/spurious, grey hatched missed)
- **over-captured characters** (the extra `.`) get a distinct yellow tint inside the predicted span

Filters: class · entity · tag · mismatches only · **near-misses only**
(coverage ≥ 90% but not `exact`) — that last one is “show me everything that
is really right but scored wrong on strict”.

Actions queue a **draft** overlay or a corpus patch: `+ exclude term`,
`+ context cue`, `+ number rule`, **accept as expected**, **mark advisory**.
Nothing writes production rules.

---

## API

Dashboard auth + CSRF required. See
[`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl).

| Method | Path |
|---|---|
| `POST` | `/api/gateway/evaluations/run` |
| `POST` | `/api/gateway/evaluations/run/stream` (NDJSON; disconnect cancels later cases) |
| `GET` | `/api/gateway/evaluations/runs` |
| `GET` | `/api/gateway/evaluations/runs/{run_uuid}` |
| `GET` | `/api/gateway/evaluations/runs/{run_uuid}/cases/{case_id}` |
| `GET` | `/api/gateway/evaluations/compare?a={run_uuid}&b={run_uuid}` |
| `POST` | `/api/gateway/evaluations/corpus-patch` |

`POST /run` extra body fields: `draft_rules`, `pack`, `normalization`, `tier`
(string or list), `persist`, `label`, `run_uuid`. Explorers may POST
`/api/gateway/*` (compute-only).

---

## What this is not

| This | That |
|---|---|
| Free-text span eval (`redibis pii eval`) | Table-column eval (`redibis eval`, [`TABLE_COLUMN_EVAL.md`](TABLE_COLUMN_EVAL.md)) |
| Scoring detections | Scanning / de-id ([`TEXT_PII_SCAN.md`](TEXT_PII_SCAN.md)) |
| Draft rules / corpus patch | Production `PUT /api/pii/text/rules` |
| In-memory run registry | Durable pack-store history (Plan 2) |

Pack `--pack` accepts a filesystem directory/archive **or** `id@version` /
`family_id@version` from the pack store. Missing path and unknown store id
both raise `EvalPinError`. Unpinned `rules_source: config+stored` is stamped
`rules_unpinned: true` with a header warning.
