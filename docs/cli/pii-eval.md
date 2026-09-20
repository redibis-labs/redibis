# CLI help — `redibis pii eval` / `eval-build`

Score free-text PII spans. Does not write contracts.

**Feature:** [`TEXT_PII_EVAL.md`](../TEXT_PII_EVAL.md)  
**Tutorial:** [`tutorials/TEXT_PII_EVAL_TUTORIAL.md`](../tutorials/TEXT_PII_EVAL_TUTORIAL.md)

```bash
redibis pii eval --help
redibis pii eval-build --help
```

## Build datasets from authored corpora

```bash
redibis pii eval-build \
  --corpus tests/data/text_pii/corpora \
  --out tests/data/text_pii/datasets
```

YAML rows are **values**, not offsets. A value that is not a verbatim substring
of `text` fails with the case id (exit 1).

## Score

```bash
redibis pii eval \
  --dataset tests/data/text_pii/datasets \
  --recursive \
  --rules configs/text_rules.default.yaml \
  --normalization v1 \
  --tier strict,value,overlap,type \
  --gate-file tests/data/text_pii/gates/all.yaml \
  --engines regex \
  --out-dir reports/eval
```

| Flag | Why |
|---|---|
| `--dataset FILE\|DIR` | portable JSON (`kind: redibis.text_span_eval_dataset`) |
| `--recursive` | walk nested `*.json` when `--dataset` is a folder |
| `--rules FILE` | pin overlay (skips persisted `pii_text_rules`) |
| `--rules-defaults` | shipped defaults only |
| `--draft-rules FILE` | extra overlay; never writes production |
| `--pack` / `--pack-stack` | filesystem pack path, or `id@version` from the pack store |
| `--normalization v1` | value-tier profile id (stamped on the report) |
| `--tier a,b,…` | compute and emit only these tiers (`strict,value,overlap,type`) |
| `--gate-file FILE` | per-entity / class-budget / guard thresholds |
| `--baseline FILE` | previous `evaluation-report.json` for regression Δ |
| `--min-exact-f1 N` | exit 1 when **strict/exact** micro F1 &lt; N |
| `--run-uuid` / `--label` | name the run |
| `--out` / `--html` / `--out-dir` | JSON, HTML, or both (`evaluation-report.*`) |
| `--engines` | `regex` \| `ner` \| `both` \| `phone` or a comma list |
| `--overlap-iou` | overlap-tier threshold (default 0.5) |
| `--use-llm` / `--llm-provider` / `--llm-model` | refiner; fail-closed if unbound |

Exit: `0` ok · `1` file / gate / threshold / empty · `2` missing path / unreadable dest.

## Make

```bash
make eval-build    # corpora → datasets
make eval-check    # pytest corpus-sync
make eval          # rebuild + score with --rules configs/text_rules.default.yaml
```

## See also

- Scan / de-id: `redibis pii text`, `redibis pii deid` — [`TEXT_PII_SCAN.md`](../TEXT_PII_SCAN.md)
- Table columns: `redibis eval` — [`TABLE_COLUMN_EVAL.md`](../TABLE_COLUMN_EVAL.md)
