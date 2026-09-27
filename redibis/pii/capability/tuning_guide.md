# Adding and tuning PII detection

_Last updated: 2026-09-27_

How to add a new PII type or change how an existing one is detected — for CSV
columns, edge rules and the Text Gateway — and how to prove the change in the
[capability contract](#6-prove-it-in-the-capability-contract). Every recipe below
was run against the engine.

**The loop:** privacy/legal requests a type (template in the contract document) →
technology changes configuration (no source edits) → adds the examples to the
contract → `redibis pii capabilities verify` passes → a new contract document is
issued from the Reports page.

| You want to… | Change | Section |
|---|---|---|
| detect a new value format in CSV columns | regex override (`pii.regex_overrides`) | [1](#1-csv-columns-regex-catalogue) |
| stop a pattern that over-fires | regex override `remove` | [1](#1-csv-columns-regex-catalogue) |
| make the column name decide | `context_hints` of the pattern | [2](#2-column-names-and-context) |
| correct a verdict from column semantics | edge rule (policy pack or overlay) | [3](#3-edge-rules) |
| find names / places / organisations in text | NER model and labels | [4](#4-ner-and-llm-optional-models) |
| detect or ignore something in free text | Text Gateway rules (`text_gateway.rules`) | [5](#5-text-gateway) |

---

## 1. CSV columns: regex catalogue

The shipped catalogue (`redibis/pii/regex_catalog.py`) is read-only. Add, replace or
disable patterns with **regex overrides** — in the configuration file, as a saved
profile, or through the API (`POST /api/pii/regex/overrides`).

```yaml
pii:
  regex_overrides:
    add:
      eg_commercial_registry:
        pattern: "^CR-\\d{6}$"
        entity_type: EG_COMMERCIAL_REGISTRY
        recognizer_group: structured          # structured = one value per cell; free_text = inside text
        presidio_score: 0.9
        context_hints: [registry, commercial, سجل_تجاري]
    remove:
      - msisdn_egypt_vodafone                 # disable an over-firing pattern by name
```

Verified: a `commercial_registry` column with `CR-123456, CR-234567, …` is **not
detected** without the override and **detected as `EG_COMMERCIAL_REGISTRY`** with it.

- `redibis pii regex list` and `redibis pii regex export` show the effective
  catalogue (names, entity types, scores, validators).
- `requires_validator` attaches a checksum (for example `validate_luhn`,
  `validate_iban`) so a match counts only when the value is valid.
- `replace_all: true` ignores the shipped catalogue — use only for isolated tests.
- A new `entity_type` becomes a governed type (sensitivity, masking, contract tags)
  once it has a mapping in `canonical_entity()` (`redibis/models.py`).

## 2. Column names and context

`context_hints` are column-name tokens, in English or Arabic, that confirm a
pattern. They matter most for shapes that are common: nine digits are a tax ID only
under a tax column; five digits are a postcode candidate only under a postal
column. The contract's *Column names and context* section shows these cases.

Patterns that share a shape across different types belong to a **collision group**
(for example 16 digits: a card number or an IMEISV). The column name picks the
winning type; the other formats of that same type (Visa, Mastercard, spaced card
numbers) keep running.

## 3. Edge rules

Edge rules correct a verdict from column semantics — never from individual values.
They live in the active policy pack (`classification.policy_pack`, `telecom` by
default) under `edge_rules:`; the full reference is `docs/EDGE_RULES_GUIDE.md`.

```yaml
edge_rules:
  - id: loyalty_card_is_indirect
    when:
      name_matches: "(?i)loyalty"
    then:
      set_entity: LOYALTY_ID
      note: loyalty card number identifies a customer
```

Verified: a `loyalty_card_no` column (`L-1001, L-1002, …`) is not detected without
the rule, and detected as `LOYALTY_ID` with it; the decision path records
`edge_rule:loyalty_card_is_indirect`.

- Retune one built-in rule without copying the pack: an overlay rule with the
  **same `id`** replaces it (user packs, or `edge_rules_overlay` for one run).
- Prefer a name **and** evidence (`entity`, `nid_checksum`, `in_range`) over a name
  alone; use `require_review: true` for name-only promotions.
- Turn rules off with `classification.edge_rules_enabled: false`; switch packs with
  `classification.policy_pack`.

## 4. NER and LLM (optional models)

- **NER** finds names, places and organisations inside free text. Install a model
  (Settings → NER Models, `pii.ner.model_path`, or `REDIBIS_NER_MODEL`), then list
  the entity labels in `pii.ner.labels` — a new label phrase is detected at once.
  `redibis pii ner list` shows the models found.
- **LLM refinement** gives a second opinion on uncertain columns and spans. It is
  off by default (`pii.llm.enabled`).
- Contract cases that need a model carry `"requires": ["ner"]` (or `"llm"`) and are
  reported as *not verified* where the model is absent.

## 5. Text Gateway

Free text is scanned with the same catalogue (free-text patterns), plus Text
Gateway rules — in YAML under `text_gateway.rules`, or `GET` / `PUT
/api/pii/text/rules` (admin).

```yaml
text_gateway:
  rules:
    patterns:
      add:
        ops_ticket:
          pattern: "\\bOPS-\\d+\\b"
          entity_type: SUPPORT_TICKET
          recognizer_group: free_text
          presidio_score: 0.9
    exclude_terms: [agent, caller]            # words never reported as PII
    context_cues:
      LOCATION: {triggers: [العنوان, address], extend: sentence}
    quantity_units: [GB, MB, جنيه]            # "30 GB" is a quantity, not PII
```

Verified: “Escalated as OPS-4521 to the NOC.” returns no span by default and a
`SUPPORT_TICKET` span `OPS-4521` with the pattern above.

Try a text from the terminal: `redibis pii text "Call me on 01012345678" --engines regex`.

## 6. Prove it in the capability contract

Add the examples to the contract (`redibis/pii/capability/contract.json`, or your
own copy passed with `--contract`) as cases of the type's section:

```json
{"id": "cr-col", "kind": "column", "title": "Commercial registry numbers",
 "column": "commercial_registry", "values": ["CR-123456", "CR-234567", "CR-345678"],
 "expect": {"detected": true, "entity_type": "EG_COMMERCIAL_REGISTRY"}}
```

```json
{"id": "cr-text", "kind": "text", "title": "Registry number in a note", "language": "en",
 "text": "Registered under CR-123456 last year.",
 "expect": {"spans": [{"entity_type": "EG_COMMERCIAL_REGISTRY", "value": "CR-123456"}], "absent": []}}
```

Then:

```bash
redibis pii capabilities verify                       # every case against the engine; exit 1 if one fails
redibis pii capabilities report --format pdf -o pii-capability-contract.pdf
```

Add at least one case that must **not** be detected (`"detected": false`, or an
`absent` value) so the change cannot over-fire unnoticed.
