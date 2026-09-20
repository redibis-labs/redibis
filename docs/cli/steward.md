# CLI help — steward review / A0–A5 / verdict memory

Review an **active** contract before organisational use. Writes go through
Steward Review (same as Contracts v2). Nothing here starts a scan.

Tutorials:

- [STEWARD_REVIEW_ARTIFACTS.md](../tutorials/STEWARD_REVIEW_ARTIFACTS.md) — Finalize A0–A5 / export-verdicts
- [STEWARD_VERDICT_ATTACH.md](../tutorials/STEWARD_VERDICT_ATTACH.md) — export zip + `--steward-verdict-path` (golden CSV)

Use the **same** `--output-dir` as `scan` (default `./reports` →
`<output-dir>/_dev_storage`).

```bash
redibis steward --help
redibis steward overview --help
```

## Overview / column / verdict

```bash
redibis steward overview telecom.customers --output-dir ./reports

redibis steward column telecom.customers msisdn --output-dir ./reports
redibis steward column telecom.customers msisdn --samples --as-role admin \
  --output-dir ./reports

redibis steward verdict telecom.customers msisdn \
  --field pii --decision accept --source regex \
  --rationale engine_correct \
  --actor ada --output-dir ./reports

redibis steward verdict telecom.customers notes \
  --field pii --decision edit --source human \
  --rationale false_positive_quantity \
  --rationale-text "monetary amount, not an identifier" \
  --value '{"is_pii": false}' \
  --actor ada --output-dir ./reports
```

| `--decision` | Counts as reviewed? | Blocks Finalize? |
|--------------|---------------------|------------------|
| `accept` / `edit` / `no_action` | yes | no |
| `needs_review` / `reject` | no (needs_review is a status) | **yes** |
| *(never called)* `pending` | no | **yes** |

`--rationale other` requires `--rationale-text`. `--value` may be JSON
(`{"is_pii": true, "entity_type": "EMAIL_ADDRESS"}`) or a string (definitions).

`--source`: `regex` | `ner` | `phone` | `custom_rule` | `llm` |
`llm_synthesis` | `human` — must exist in the generations ledger unless
`human`.

## Export verdicts only (no Finalize)

PII memory for **CLI scan** without packaging A0–A5. Edits already persist in
the store; this writes a file for `--verdicts` / another host.

```bash
redibis steward export-verdicts telecom.customers \
  -o ./artifacts/steward_verdicts.json \
  --scan-package ./artifacts/scan_verdicts.json \
  --actor ada --output-dir ./reports
```

| File | Kind | Use |
|------|------|-----|
| `-o` | `redibis.steward_verdicts` (A1) | Audit + `scan decide --verdicts` + `verdict import` |
| `--scan-package` | `redibis.verdict_package` | PII-only; same consumers |

```bash
redibis scan decide --table telecom.customers --latest --preset reporting \
  --verdicts ./artifacts/steward_verdicts.json --output-dir ./reports

redibis verdict import telecom.customers \
  --package ./artifacts/steward_verdicts.json \
  --actor ada --reason "from steward review" --output-dir ./reports
```

`--verdicts` is **run-scoped** (does not write the overlay). `verdict import`
**does** write the overlay after fingerprint checks.

Attach the same file (or a directory of them) on the next scan / enrich so
human-verified columns stay locked and the engine fills the rest:

```bash
redibis scan data.csv telecom.customers --mode pii --automerge pii \
  --steward-verdict-path ./artifacts/steward_verdicts.json --output-dir ./reports

redibis enrich telecom.customers --provider demo \
  --steward-verdict-path ./artifacts/verdicts/ --output-dir ./reports
```

Same-store next scan needs **no file**:

```bash
redibis scan data.csv telecom.customers --mode pii --automerge pii --output-dir ./reports
```

## Finalize A0–A5

Blocked until every column and table item (name, description, owner, table
quality rules) is reviewed. Exit code **1** when blocked.

```bash
redibis steward finalize telecom.customers --actor ada \
  --out-dir ./artifacts/a0a5 --output-dir ./reports
```

| Artifact | `--artifact` | Typical next step |
|----------|----------------|-------------------|
| A0 guaranteed contract | `contract` | catalogue / `show` |
| A1 verdict memory | `verdicts` | `scan decide --verdicts` |
| A2 evidence + rationale | `evidence` | audit |
| A3 LLM context pack | `llm-context` | `enrich --context-pack` |
| A4 fine-tune corpus | `corpus` | `training export --label-source steward_review` |
| A5 knowledge graph | `graph` | graph tools |

```bash
redibis steward export telecom.customers --artifact verdicts -o A1.json --output-dir ./reports
redibis steward export telecom.customers --artifact contract -o A0.json --output-dir ./reports

redibis enrich telecom.customers --provider demo \
  --context-pack ./artifacts/a0a5/llm_context --output-dir ./reports

redibis training export --table telecom.customers \
  --label-source steward_review --spans --out ./finetune --output-dir ./reports
```

Storage layout: `_meta/steward_artifacts/{table}/{review_digest}/`.

## Import A1 into this store

```bash
redibis steward import-verdicts telecom.customers ./artifacts/steward_verdicts.json \
  --actor ada --reason "replay A1" --output-dir ./reports
```

Prefer `redibis verdict preview|import` when you need classification against
a real evidence run (matching / stale / missing / conflicting).

## See also

- [tutorials/STEWARD_REVIEW_ARTIFACTS.md](../tutorials/STEWARD_REVIEW_ARTIFACTS.md)
- [tutorials/STEWARD_VERDICT_ATTACH.md](../tutorials/STEWARD_VERDICT_ATTACH.md) — zip export + attach walkthrough
- [scan.md](scan.md) — `scan decide` presets · `--steward-verdict-path`
- [enrich.md](enrich.md) — `--context-pack`
- [EVIDENCE_REVIEW_LEDGER.md](../EVIDENCE_REVIEW_LEDGER.md)
