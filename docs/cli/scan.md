# CLI help — scan / profile / quality / deep-scan

Generate run artifacts and (optionally) merge into the **active** ODCS contract.

## Full contract → active storage

```bash
redibis scan data/customers.csv telecom.customers \
  --mode all \
  --automerge both \
  --equation balanced \
  --pii-engines both \
  --output-dir ./reports

redibis show telecom.customers --output-dir ./reports > contract.yaml
```

## Modes

| Command / `--mode` | Profile | Quality | PII |
|--------------------|---------|---------|-----|
| `scan … --mode all` | ✓ | ✓ | ✓ |
| `profile …` / `--mode profile` | ✓ | | |
| `quality …` / `--mode quality` | ✓ | ✓ | |
| `scan … --mode pii` | | | ✓ |
| `scan … --mode pii,quality` | ✓ | ✓ | ✓ |

```bash
redibis scan data.csv telecom.customers --mode pii --automerge pii --output-dir ./reports
redibis quality data.csv telecom.customers --automerge quality --output-dir ./reports
redibis profile data.csv telecom.customers --output-dir ./reports
```

## Important flags

| Flag | Values |
|------|--------|
| `--mode` | `all` \| `profile` \| `pii` \| `quality` \| comma list |
| `--automerge` | `none` (default) \| `pii` \| `quality` \| `both` |
| `--equation` | `strict` \| `balanced` \| `lenient` \| `independent` |
| `--pii-engines` | `regex` \| `gliner` \| `ner` \| `llm` \| `both` |
| `--ner-model` | local NER weights directory |
| `--config` | `redibis.yaml` |
| `--no-ge-docs` | skip GE Data Docs HTML |
| `--no-validate` | skip ODCS validation |
| `--use-s3` + `--s3-endpoint` | MinIO/S3 instead of local |
| `--steward-verdict-path` | A1 / verdict_package file, or a directory of them. Matching table+schema **human-verified** PII columns lock the overlay; `needs_review` and columns with no verdict take the engine. Same flag on `profile`, `quality`, `deep-scan`, and `enrich`. |

Without `--automerge`, merge later:

```bash
redibis runs list telecom.customers --output-dir ./reports
redibis runs merge telecom.customers <run_id> --output-dir ./reports
```

## Deep scan

```bash
redibis deep-scan data.csv telecom.customers --output-dir ./reports
redibis deep-scan data.csv telecom.customers --producers a,b --no-bundle
```

## Evidence store (replay without rescanning)

Full guide with worked examples: **[EVIDENCE_STORE.md](../EVIDENCE_STORE.md)**.

```bash
# emit / list
redibis scan evidence --table telecom.customers --latest --out -
redibis scan evidence --table telecom.customers --list-runs
redibis scan evidence coverage --table telecom.customers --latest
redibis scan evidence llm --table telecom.customers --latest
redibis scan evidence llm --table telecom.customers --call-id <id>
redibis scan evidence llm --table telecom.customers --call-id <id> \
  --raw --actor steward --reason debug --role data_steward

# persist a shareable copy (no samples.values, no frequency.top_values literals)
redibis scan evidence store --table telecom.customers --latest
redibis scan evidence store --table telecom.customers --keep-masked-samples

# replay PII verdicts at new floors — zero source-table reads
redibis scan decide --table telecom.customers --latest --preset investigation
redibis scan decide --table telecom.customers --latest --preset audit --out audit.json

# LLM envelope (refuses raw PII unless --allow-raw-pii --reason TEXT)
redibis context build --add './reports/**/evidence_bundle.shareable.json' --out context.json

# pack stack that ran + air-gap export
redibis scan evidence packs --table telecom.customers --latest
redibis scan evidence export --table telecom.customers --with-packs --out bundle.zip
redibis pack get <uuid> --out ./packs/ --extract
redibis pack diff <uuid-a> <uuid-b>
redibis get llm-call-logs <run_id> --zip evidence.zip --output-dir ./reports
```

| Preset | Equation | Optimises |
|--------|----------|-----------|
| `investigation` | lenient (presidio 0.45, gliner 0.30) | recall |
| `reporting` | balanced (defaults) | balance |
| `audit` | strict (presidio 0.92, gliner 0.85) | precision |

## See also

- [../EVIDENCE_STORE.md](../EVIDENCE_STORE.md) — evidence bundle, store, decide, context, packs
- [../tutorials/STEWARD_REVIEW_ARTIFACTS.md](../tutorials/STEWARD_REVIEW_ARTIFACTS.md) — steward A1 `--verdicts` / Finalize A0–A5
- [../tutorials/STEWARD_VERDICT_ATTACH.md](../tutorials/STEWARD_VERDICT_ATTACH.md) — zip export + `--steward-verdict-path` (golden CSV)
- [contract.md](contract.md) — show / PII YAML-JSON export
- [enrich.md](enrich.md) — LLM enrichment after active contract exists
- [config-batch.md](config-batch.md) — YAML config + folder batch
- Full tour: [../CLI_SCAN_ENRICH_TOUR.md](../CLI_SCAN_ENRICH_TOUR.md)
