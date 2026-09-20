![redibis — easily govern](redibis/webapp/static/logo.png)

# redibis

**Continuous data protection and ODCS data-contract generation** for open-source
pipelines: scan → contract → enrich → mask → catalog.

Put redibis at the heart of your CI/CD and monitoring so tables stay classified,
quality-gated, and safe to share.

The name combines **"redact" + "ibis"** — the Egyptian ibis was Thoth's sacred
bird, the symbol of writing, records, and the categorization of knowledge.

This repository is the **Apache-2.0 open core**. Commercial add-ons (hosted
codegen, filtered reports console, air-gapped enterprise bundles) are separate
products and are not included here.

---

## Introduction

`redibis` turns a raw table into a governed, enriched **ODCS v3 data contract**,
then helps you produce a safe-to-share copy of the data:

1. **Scan** — profile data quality (Great Expectations) and detect PII
   (Presidio + GLiNER, optional LLM).
2. **Contract** — write per-run *subcontracts*, then smart-**merge** them into a
   single active contract.
3. **Steward** — human review overlays (PII decisions, Finalize A0–A5, portable
   verdict memory).
4. **Enrich** — use any LLM (via LiteLLM) to add a business glossary and refine
   classifications — validity-gated.
5. **Mask** — de-identify columns with mask / hash / encrypt / **NIST FF3-1 FPE**
   / locale-aware fakers before sharing or sending to an LLM.
6. **Catalog** — push contracts to OpenMetadata (and related catalog sinks).

> **Start here:** **[docs/tutorials/INTRO.md](docs/tutorials/INTRO.md)** —
> dashboard start/stop, CSV scan, enrich, evidence, quality, OpenMetadata.  
> CLI cheat sheets: **[docs/cli/README.md](docs/cli/README.md)**.  
> Full CLI + Jupyter tour: **[docs/tutorials/PIPELINE_GUIDE.md](docs/tutorials/PIPELINE_GUIDE.md)**.

---

## Architecture

Dependencies flow one way toward `cli` / `webapp`. Domain packages never import
`redibis.cli`.

```text
CSV / Parquet / DataFrame
        │
        ▼
   ScanService  ──►  run artifacts (evidence, reports)
        │
        ├─► quality subcontract (GE)
        └─► PII subcontract (Presidio / GLiNER / phone / rules)
                │
                ▼
        ContractStore.upsert()   ◄── single writer to contracts bucket
                │
                ├─► PiiDecisionStore / steward overlays (human wins)
                ├─► enrich (LLM delta, scrubbed)
                └─► mask / catalog / quality-monitor
```

### Modular packages

| Package | Responsibility |
|---------|----------------|
| `redibis` (facades) | Preferred library entry — `PIIScan`, `QualityScan`, `ProfileScan` |
| `redibis.scan` | Scan orchestrator, report bundle, subcontract writer |
| `redibis.store` | ODCS storage, smart upsert, audit trail — **only** contracts-bucket writer |
| `redibis.quality` | GE sampling, gatekeeper, quality ODCS export |
| `redibis.pii` | Presidio + GLiNER, equation modes, PII ODCS export |
| `redibis.profiling` | Pluggable profilers (GE, OpenMetadata, DuckDB SUMMARIZE) |
| `redibis.contracts` | ODCS mapping, privacy/retention helpers |
| `redibis.masking` | Mask / hash / AES-GCM / FF3-1 FPE / EN+AR fakers |
| `redibis.enrich` | Full-contract LLM enrichment ([docs/LLM_ENRICHMENT.md](docs/LLM_ENRICHMENT.md)) |
| `redibis.services` | `ScanService`, browse/contract/masking services for CLI + web |
| `redibis.webapp` | FastAPI dashboard |
| `redibis.agents` | Optional agentic board (Ask / Composer) behind `agents.enabled` |

### Design invariants

- **Single writer** — `ContractStore.upsert()` is the only path into the contracts bucket.
- **Identity locking** — `contract_uuid + database_name + table_name` fixed after first write.
- **PII decision overlay** — demotions go through `PiiDecisionStore`; the merger cannot strip PII alone.
- **Evidence vs verdict** — detectors return evidence; the equation engine produces the verdict.
- **Safe rules parsing** — pasted GE code is `ast.parse` + `ast.literal_eval` only (never `exec`).
- **Per-run masking keys** — minted per apply; never written into exported data.
- **Enrichment output scrub** — written contracts scrub PII samples and quality rules on PII columns.
- **Steward attach** — `--steward-verdict-path` locks human-verified columns; engines fill the rest.

---

## Features

- ODCS v3 active contracts with versioned audit trail
- Profile / quality / PII scan modes (`all`, `profile`, `pii`, `quality`, comma lists)
- Equation modes: `strict` · `balanced` · `lenient` · `independent`
- Local GLiNER / NER weights (BYO model; no runtime Hugging Face download)
- Steward Review (Contracts v2): accept / edit / needs_review, Finalize A0–A5,
  export verdicts, export artifact zip, `--steward-verdict-path` on scan/enrich
- LLM enrich (vLLM, Ollama, SGLang, Gemini, demo) + multistep / batch
- Evidence store, packs, `scan decide`, portable verdict packages
- Masking / FPE / locale-aware fakers (EN + AR)
- Quality monitor + rule export (GE / SodaCL / dbt)
- OpenMetadata catalog push
- Optional agentic UI at `/agents` (disabled by default in locked-down configs)
- Local filesystem storage by default (optional MinIO/S3)

---

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .                 # core
pip install -e ".[ge]"           # Great Expectations profiling
pip install -e ".[ner-runtime]"  # Presidio + GLiNER libraries (no model weights)
pip install -e ".[enrich]"       # LiteLLM-backed enrichment
pip install -e ".[mask]"         # faker, cryptography, pyarrow
pip install -e ".[web]"          # FastAPI dashboard

# Recommended local dev:
pip install -e ".[dev,web]" -c requirements/constraints.txt

# Pinned full stack:
pip install -r requirements/requirements-all.txt && pip install -e . --no-deps

# Interactive setup (Conda or Pip/Venv):
./scripts/setup_dev_env.sh
```

After `pip install -e .`, the `redibis` console script is on your `PATH`
(also: `python -m redibis.cli.main`).

---

## Start / stop (dashboard)

Local filesystem (no MinIO):

```bash
export USE_LOCAL_STORAGE=true
export LOCAL_STORAGE_ROOT=./_local_storage
export SCAN_OUTPUT_DIR=./scan_output

python -m redibis.webapp.backend
# equivalent: uvicorn redibis.webapp.backend:app --reload --host 127.0.0.1 --port 8000
```

**Dev helper** (`./scripts/webapp.sh`):

```bash
./scripts/webapp.sh --start                         # foreground (+ reload)
./scripts/webapp.sh --start --background            # detached → .run/webapp.log
./scripts/webapp.sh --start --background --monitor
./scripts/webapp.sh --restart
./scripts/webapp.sh --stop
./scripts/webapp.sh --status
./scripts/webapp.sh --start --venv --ner            # .venv + local GLiNER weights
./scripts/webapp.sh --logs
```

Open **http://localhost:8000/** (scan), **/v2** (Contracts v2 / Steward Review),
**/settings**, **/agents** (agentic Ask — optional), **/docs** (Swagger, session
required). Auth is **on by default** — empty `users.json` bootstraps
`admin` / `admin` (change it at `/users`). Prefer binding to loopback for local
use; put internet-facing deploys behind HTTPS.

Ops detail: [docs/WEB_ADMIN.md](docs/WEB_ADMIN.md) ·
[docs/DASHBOARD_AUTH.md](docs/DASHBOARD_AUTH.md) ·
[docs/DEPLOY.md](docs/DEPLOY.md).

| Variable | Meaning |
|----------|---------|
| `USE_LOCAL_STORAGE=true` | Force local filesystem backend |
| `LOCAL_STORAGE_ROOT` | Contract/backend root (default `./_local_storage`) |
| `SCAN_OUTPUT_DIR` | Web session root (default `./scan_output`) |
| CLI `--output-dir` | CLI artifacts + `_dev_storage` (default `./reports`) |

Dashboard and CLI **do not share contracts** unless you point them at the same
storage root.

---

## Tutorials

| Doc | When to open it |
|-----|-----------------|
| **[docs/tutorials/INTRO.md](docs/tutorials/INTRO.md)** | **Start here** — start/stop, CSV scan, enrich, evidence, quality, catalog |
| [PIPELINE_GUIDE.md](docs/tutorials/PIPELINE_GUIDE.md) | CLI + Jupyter / Python API end to end |
| [CLI_SCAN_TUTORIAL.md](docs/tutorials/CLI_SCAN_TUTORIAL.md) | `redibis scan` on golden CSVs |
| [STEWARD_REVIEW_ARTIFACTS.md](docs/tutorials/STEWARD_REVIEW_ARTIFACTS.md) | Finalize A0–A5 / export verdicts |
| [STEWARD_VERDICT_ATTACH.md](docs/tutorials/STEWARD_VERDICT_ATTACH.md) | Zip export + `--steward-verdict-path` |
| [PII_CSV_ENRICH_OPENMETADATA_CLI.md](docs/tutorials/PII_CSV_ENRICH_OPENMETADATA_CLI.md) | PII → enrich → OpenMetadata |
| [SCAN_ENRICH_CATALOG_SGLANG.md](docs/tutorials/SCAN_ENRICH_CATALOG_SGLANG.md) | Same path with local SGLang |
| [QUALITY_WORKFLOW_TUTORIAL.md](docs/tutorials/QUALITY_WORKFLOW_TUTORIAL.md) | Quality discovery → monitor |
| [CLI_QUALITY_SCAN_TUTORIAL.md](docs/tutorials/CLI_QUALITY_SCAN_TUTORIAL.md) | Quality CLI only |
| [QUALITY_SCAN_PYTHON_TUTORIAL.md](docs/tutorials/QUALITY_SCAN_PYTHON_TUTORIAL.md) | `QualityScan` / `ScanService` in Python |
| [TEXT_PII_EVAL_TUTORIAL.md](docs/tutorials/TEXT_PII_EVAL_TUTORIAL.md) | Free-text PII eval |
| [docs/agents/howto/](docs/agents/howto/README.md) | Agentic Ask / Composer / safety |

Index: [docs/tutorials/README.md](docs/tutorials/README.md).

### Minimal golden-CSV path

```bash
export OUT=./reports
export CSV=tests/data/golden_tutorial_customers.csv
export TABLE=golden.tutorial_customers

redibis scan "$CSV" "$TABLE" --mode pii --automerge pii --output-dir "$OUT"
redibis show "$TABLE" --output-dir "$OUT"

# Steward memory for the next host / store:
redibis steward export-verdicts "$TABLE" -o ./artifacts/steward_verdicts.json \
  --actor ada --output-dir "$OUT"

redibis scan "$CSV" "$TABLE" --mode pii --automerge pii \
  --steward-verdict-path ./artifacts/steward_verdicts.json \
  --output-dir ./reports/replay
```

---

## CLI documentation

Full cheat sheets live under **[docs/cli/](docs/cli/README.md)**:

| Topic | Page |
|-------|------|
| Scan / profile / quality / deep-scan | [scan.md](docs/cli/scan.md) |
| Steward review / A0–A5 / attach | [steward.md](docs/cli/steward.md) |
| Contracts & PII export | [contract.md](docs/cli/contract.md) |
| Contract synthesis | [synthesize.md](docs/cli/synthesize.md) |
| LLM enrich | [enrich.md](docs/cli/enrich.md) |
| Multistep / batch enrich | [multi-and-batch-enrich.md](docs/cli/multi-and-batch-enrich.md) |
| LLM list / test | [llm.md](docs/cli/llm.md) |
| Catalog → OpenMetadata | [catalog.md](docs/cli/catalog.md) |
| Quality monitor | [monitor.md](docs/cli/monitor.md) |
| Config & batch | [config-batch.md](docs/cli/config-batch.md) |
| Mask | [mask.md](docs/cli/mask.md) |
| Free-text PII eval | [pii-eval.md](docs/cli/pii-eval.md) |

Tour: [docs/CLI_SCAN_ENRICH_TOUR.md](docs/CLI_SCAN_ENRICH_TOUR.md) ·
Providers: [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md) ·
Evidence: [docs/EVIDENCE_STORE.md](docs/EVIDENCE_STORE.md).

```bash
redibis --help
redibis <command> --help
redibis <command> <action> --help
```

Use the **same** `--output-dir` (default `./reports`) on every command in a
workflow so scan / show / enrich / steward / catalog share one local store
(`<output-dir>/_dev_storage`).

### Copy-paste CLI workflow

```bash
# 1. Scan → active contract
redibis scan data.csv telecom.customers --mode all --automerge both --output-dir ./reports

# 2. Review runs (when not auto-merging)
redibis runs list telecom.customers --kind pii --output-dir ./reports
redibis runs merge telecom.customers --kind pii --output-dir ./reports

# 3. Probe LLM, then enrich
redibis llm list
redibis llm test demo
pip install -e ".[enrich]"
redibis enrich telecom.customers --provider demo --output-dir ./reports
redibis enrich telecom.customers --provider vllm --multistep --output-dir ./reports

# 4. Mask for safe sharing
redibis mask auto data.csv --table telecom.customers --out data_safe.csv --output-dir ./reports

# 5. Inspect
redibis show telecom.customers --output-dir ./reports
redibis history telecom.customers --output-dir ./reports
redibis list --output-dir ./reports
redibis rules export telecom.customers --target ge --output-dir ./reports
```

`--mode` accepts `all`, `profile`, `pii`, `quality`, or a comma list (`pii,quality`).

**Common commands:** `scan`, `profile`, `quality`, `deep-scan`, `monitor`,
`runs`, `rules`, `contract`, `steward`, `verdict`, `enrich`, `llm`, `mask`,
`catalog`, `config`, `show`, `history`, `list`, `models`.

### PII scan with GLiNER

Weights are **not** bundled. Download once, then point redibis at the folder:

```bash
pip install -e ".[ner-runtime]" -c requirements/constraints.txt
./scripts/download_ner_model.sh    # → models/gliner-multi-v2.1/

export REDIBIS_MODELS_DIR=./models
export REDIBIS_NER_MODEL=./models/gliner-multi-v2.1

redibis scan data.csv telecom.customers --mode pii --pii-engines both \
  --ner-model models/gliner-multi-v2.1 --output-dir ./reports
```

Tuning: [docs/PII_DETECTION_TUNING.md](docs/PII_DETECTION_TUNING.md).

---

## Quick start (Python)

```python
from pathlib import Path
import pandas as pd
from redibis import PIIScan, QualityScan, ProfileScan, ScanConfig
from redibis.config import NERConfig

df = pd.read_csv("tests/data/golden_tutorial_customers.csv")
cfg = ScanConfig(
    table="golden.tutorial_customers",
    equation_mode="balanced",
    ner_config=NERConfig(model_path="models/gliner-multi-v2.1"),  # optional
)
pii = PIIScan(cfg).run(df, run_dir=Path("./reports"))
print(pii.status, pii.flagged_columns)
```

Persistence (CLI-equivalent):

```python
from pathlib import Path
from redibis.store.storage_backend import LocalBackend
from redibis.store.contract_store import ContractStore
from redibis.store.subcontract_store import SubcontractStore
from redibis.services.scan_service import ScanService, ScanConfig

backend = LocalBackend("./dev_storage")
store = ContractStore(backend, bucket="active-contracts")
sub_store = SubcontractStore(
    backend, pii_bucket="pii-contracts", quality_bucket="quality-contracts",
)
scan = ScanService(backend, store, runs_bucket="pii-reports", sub_store=sub_store)
result = scan.scan_csv(
    "tests/data/golden_tutorial_customers.csv",
    ScanConfig(
        table="golden.tutorial_customers",
        run_pii=True, run_quality=True,
        automerge="both",
        output_dir=Path("./reports"),
    ),
)
print(result.status, store.get_active("golden.tutorial_customers")["version"])
```

More: [docs/tutorials/PIPELINE_GUIDE.md](docs/tutorials/PIPELINE_GUIDE.md).

---

## Commercial add-ons

The open-source core writes scan report artifacts under the run folder. The
filtered KPI console and CSV downloads at `/reports` are provided by the
separately licensed `redibis-reports` add-on (`pip install redibis-reports`).
Hosted codegen and air-gapped enterprise bundles are likewise separate products.

---

## Contributing & security

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)

```bash
pytest tests/ -q
```

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
