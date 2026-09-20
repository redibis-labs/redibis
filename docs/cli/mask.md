# CLI help — mask (de-identify)

Produce a safe-to-share copy of a CSV/Parquet after (or without) a PII scan.

```bash
pip install -e ".[mask]" -c requirements/constraints.txt
```

## One-shot auto (suggest from PII + apply)

```bash
redibis mask auto data/customers.csv \
  --table telecom.customers \
  --out data_safe.csv \
  --from-pii
```

## Plan → apply

```bash
redibis mask plan data/customers.csv --table telecom.customers --from-pii --out plan.yaml
# edit plan.yaml if needed
redibis mask apply data/customers.csv --plan plan.yaml --out data_safe.csv
```

## Regex library (fake kind=regex)

```bash
redibis mask regex list
redibis mask regex test PATTERN_NAME --n 5
redibis mask capabilities
```

## Notes

- Keys are minted **per run** and are **not** written into the export or manifest.
- Prefer masking before sending samples to a **cloud** LLM (`--external-masked-ack` on enrich).

## See also

- [../masking_guide.md](../masking_guide.md)
- [../data_masking.md](../data_masking.md)
- [enrich.md](enrich.md)
