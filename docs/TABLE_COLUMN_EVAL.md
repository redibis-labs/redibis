# Table-column evaluation

Score **engine**, **active contract**, or **LLM proposal** output against a
portable, value-free expected matrix. This is the structured counterpart of
Gateway Evaluation Builder
([`TEXT_PII_EVAL.md`](TEXT_PII_EVAL.md),
[`tutorials/TEXT_PII_EVAL_TUTORIAL.md`](tutorials/TEXT_PII_EVAL_TUTORIAL.md)).
It never writes a contract (`ContractStore.upsert()` stays the only writer).

The UI lives on **Data → Evaluation** after a table is loaded. Labels stay in
the browser session. Import/export uses JSON only — no cell values.

## Dataset contract

Kind: `redibis.table_column_eval_dataset` · schema `1.0`.

```json
{
  "kind": "redibis.table_column_eval_dataset",
  "schema_version": "1.0",
  "table_name": "demo.customers",
  "fingerprint": "optional-structural-hash",
  "residency": "portable",
  "columns": [
    {
      "name": "email",
      "is_pii": true,
      "entity_type": "EMAIL_ADDRESS",
      "logicalType": "string",
      "privacy_classification": "CONFIDENTIAL",
      "businessName": "Customer email",
      "description": "",
      "business_definition": "",
      "tags": ["customer"]
    }
  ]
}
```

Required per column: `name`, `is_pii`, and `entity_type` when `is_pii` is true.
Optional contract-shaped fields: `logicalType`, `privacy_classification`,
`businessName`, `description`, `business.definition` / `business_definition`,
`tags`. Sample cell values are not part of the product schema.

`redibis_version` is stamped on normalize/export. Input files are accepted when
`kind` and `schema_version` match.

Existing generator corpora under
`tests/generators/pandas/v1/table_column_golden_truth_json_package/` can be
adapted with `from_column_evaluations_corpus()`; that test-only shape is not
the production contract.

## Targets

| Target | Actuals come from | Notes |
|---|---|---|
| `engine` | `detect_pii` + `decide_pii` on the loaded frame / `--data` file | Deterministic engines/equation |
| `contract` | Latest **active** contract properties | 404 / exit 2 if none; read-only |
| `llm` | Engine detections after `refine_detections` | Requires `pii.llm.enabled`; only columns actually refined are marked as proposals |

Scoring: categorical fields use normalized exact match. Tags use set
precision/recall. Blank optional definitions are skipped. `--semantic` (opt-in)
asks an LLM judge for definition similarity through
`guarded_model_call` / `contract.enrichment`. The judge sees truncated
definition text only — never raw table values. Definition text may leave the
host when `contract.enrichment` is bound to a cloud provider, subject to the
configured RAI policy. Responses are parsed as strict JSON and routing/model
errors fail the semantic assertion closed.

Reports include target-column coverage. Missing target columns and unexpected
extra target columns affect exact F1; an empty or incomplete contract cannot
receive a perfect score simply because omitted columns were expected non-PII.
Stale fingerprints and live-table column drift block evaluation until the
matrix is revalidated.

## UI

1. Load data as usual (Data tab).
2. Open **Evaluation**.
3. Engine suggestions appear next to each column and are **not** auto-accepted.
4. Toggle PII, set entity type, optionally fill definition fields.
5. Choose target (`engine` / `contract` / `llm`), set the minimum F1, and Evaluate.
6. Export JSON (value-free) or import a matrix. Import reports missing, extra,
   or stale-fingerprint columns before scoring.

The result view shows pass/fail, engine/equation provenance, target coverage,
and field-level expected/actual differences. Changing sessions discards the
session-scoped draft.

## API

Session-scoped, no new label store:

| Method | Path |
|---|---|
| `GET` | `/api/sessions/{id}/eval/scaffold` |
| `POST` | `/api/sessions/{id}/eval/validate` |
| `POST` | `/api/sessions/{id}/eval/export` |
| `POST` | `/api/sessions/{id}/eval/run` |
| `POST` | `/api/sessions/{id}/eval/run/stream` |

The stream endpoint is NDJSON. Disconnect cancels later work; an already
running model call is bounded by the provider timeout.

## CLI / CI

```bash
redibis eval validate --dataset labels.json
redibis eval run --dataset labels.json --data customers.csv --target engine \
  --engines regex --html report.html -o report.json --min-exact-f1 0.9
redibis eval run --dataset labels.json --target contract --table demo.customers
```

Exit codes match text evaluation: `0` success, `1` below `--min-exact-f1`,
`2` missing file / invalid dataset / no active contract / unreadable output.

This is distinct from `redibis pii eval`, which scores free-text span datasets.
