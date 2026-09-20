# CLI help — config & batch

## Dump / edit config

```bash
redibis config dump-default -o redibis.yaml
# edit pii.equation_mode, pii.engines, contract.automerge, …

export REDIBIS_CONFIG=/path/to/redibis.yaml   # optional global default

redibis scan data.csv telecom.customers \
  --config redibis.yaml \
  --automerge both \
  --output-dir ./reports
```

CLI flags override matching YAML fields for that run.

### Common YAML knobs

```yaml
pii:
  engines: both                 # regex | gliner | ner | llm | both
  equation_mode: balanced
  use_phonenumbers: true
  ner:
    model_path: models/gliner-multi-v2.1

contract:
  automerge: none               # or both — still overridable with --automerge
  validate: true

report:
  output_dir: ./reports

evidence:
  restricted_spool_dir: ./reports/_restricted_evidence
  access:
    steward_role: data_steward
    require_actor: true
    require_reason: true
    audit_enabled: true

enrich:
  include_pii_evidence: false
  max_attempts: 3                 # LLM/ODCS repair retries (default 3)
  context:
    pack: ""                      # enrichment pack, .rdbpack, or exported profile DIR
    custom_dir: ""                # default: $REDIBIS_CONFIGS_DIR/enrich-context/custom
  multistep:
    enabled: false                # true = run stages without --multistep
    steps_file: ""                # or path to enrich-steps YAML
    rai_enabled: null             # null=use rai.*; false for local LLM
    review_enabled: true          # extra whole-contract LLM pass
    review_max_changes: 25
  similar_context:
    enabled: true
    k: 5
```

Example snippets: `config/examples/` (including `enrich-steps-default.yaml`,
`catalog-openmetadata.yaml`).

## Batch — folder of CSVs

```bash
#!/usr/bin/env bash
set -euo pipefail
IN_DIR="${1:-./data}"
OUT="${2:-./reports/batch}"
mkdir -p "$OUT"

for csv in "$IN_DIR"/*.csv; do
  table="telecom.$(basename "$csv" .csv)"
  redibis scan "$csv" "$table" \
    --mode all --automerge both --equation balanced \
    --output-dir "$OUT" --config ./redibis.yaml || continue
  redibis show "$table" --output-dir "$OUT" > "$OUT/${table}.contract.yaml"
  redibis contract pii-view "$table" --json --output-dir "$OUT" \
    > "$OUT/${table}.pii.json"
done
```

Packaged helper (filename `schema_table….csv` → `schema.table`):

```bash
./scripts/batch_scan.sh --input /data/csvs --output ./scan_output --mode both --auto-write
```

### Batch enrich

How-to (single-shot, `--multistep`, and batch + multistep):
[multi-and-batch-enrich.md](multi-and-batch-enrich.md).

```bash
./scripts/batch_enrich.sh -o ./scan_output --provider sglang

OUT=./scan_output
for table in telecom.customers telecom.orders; do
  redibis enrich "$table" --provider vllm --endpoint http://127.0.0.1:8080/v1 \
    --output-dir "$OUT" || echo "failed: $table"
done

# Or multistep (table definition + whole-contract review + stage retries)
redibis enrich telecom.customers --provider vllm --multistep \
  --steps-file config/examples/enrich-steps-default.yaml \
  --output-dir "$OUT"

# Export the reusable prompt profile once, then reuse as --pack
redibis enrich export-context ./context-profile --output-dir "$OUT"
redibis enrich add-context glossary.md --scope global --mode shared --output-dir "$OUT"
```

### Batch catalog push (scan folder)

```bash
OUT=./scan_output
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml
export OM_BOT_JWT='...'

redibis catalog push-scan "$OUT" --output-dir "$OUT"
redibis catalog push-scan "$OUT" --resume <run_id>   # after partial failures
```

See [catalog.md](catalog.md).

## Batch — continuous quality monitor

After contracts are merged, schedule **validate-only** checks (distinct from discovery
`batch_scan.sh`):

```bash
redibis quality-monitor batch --all-contracts --config ./redibis.yaml --use-s3 --json
redibis quality-monitor airflow generate --all-contracts -o ./dags --config ./redibis.yaml
```

See [monitor.md](monitor.md).

## See also

- [scan.md](scan.md) · [enrich.md](enrich.md) · [monitor.md](monitor.md) · [multi-and-batch-enrich.md](multi-and-batch-enrich.md) · [llm.md](llm.md) · [catalog.md](catalog.md)
- [../CLI_SCAN_ENRICH_TOUR.md](../CLI_SCAN_ENRICH_TOUR.md) §3 / §7
- [../SCAN_CSV_TO_CONTRACT_GUIDE.md](../SCAN_CSV_TO_CONTRACT_GUIDE.md)
- [../tutorials/CATALOG_OPENMETADATA_TUTORIAL.md](../tutorials/CATALOG_OPENMETADATA_TUTORIAL.md)
