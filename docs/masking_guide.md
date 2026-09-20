# Masking & De-identification — Index

The full programmatic reference (Python API, CLI, REST API, manifest, audit
report, record preview) lives in:

**→ [`data_masking.md`](data_masking.md)**

That document is the canonical source for:

- Strategy reference (`passthrough`, `mask`, `hash`, `encrypt`, `fpe`, `fake`, regex)
- Plan YAML schema and PII auto-suggest mapping
- Export sidecars: manifest v2 + audit report v1
- Single-record preview API (`?row=` / `compare_record`)
- All CLI commands (`redibis mask …`, `redibis data preview`)
- All REST endpoints under `/api/sessions/{sid}/mask/…` and `/api/masking/…`

Install extras for production crypto and fakers:

```bash
pip install "redibis[mask]"
```

Quick start (CLI):

```bash
redibis mask plan data.csv --from-pii --out plan.yaml
redibis mask apply data.csv --plan plan.yaml --out data_safe.csv
# → data_safe.csv, data_safe.csv.manifest.json, data_safe.csv.audit.json
```
