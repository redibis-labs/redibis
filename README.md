# redibis — open-source data contract pipeline

**ODCS v3 contracts** · Great Expectations quality · Presidio + GLiNER PII ·
LLM enrichment · masking / FPE · Arabic + English.

## Install

```bash
pip install -e ".[dev]" -c requirements/constraints.txt
redibis --help
```

## Quick start

```python
from redibis import PIIScan, QualityScan, ProfileScan
from redibis.scan import ScanConfig

result = PIIScan(ScanConfig(table="demo.customers")).run(df)
```

```bash
redibis scan --file data.csv --table demo.customers --mode both
```

## Dashboard

```bash
pip install -e ".[web]"
python -m uvicorn redibis.webapp.backend:app --host 127.0.0.1 --port 8000
```

Authentication is enabled by default. Change bootstrap credentials before
binding beyond loopback, and place internet-facing deployments behind HTTPS.

## Commercial reporting

The open-source core generates scan report artifacts. The filtered KPI console,
CSV downloads, and compute debug stream at `/reports` are provided by the
separately licensed `redibis-reports` add-on:

```bash
pip install redibis-reports
```

Restart the web application after installing the add-on. The add-on source and
wheel are not distributed under the Apache-2.0 license.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.

Commercial add-ons (hosted codegen, reports console, air-gapped deploy bundles)
are separate products and are **not** included in this repository.
