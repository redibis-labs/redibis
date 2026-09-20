# NER Models — Download, Package & Upload

How to obtain GLiNER (or other compatible NER) weights, place them where
redibis expects them, and register them for PII scans.

**Surfaces covered:** offline download, manual install, CLI upload, REST API,
Docker mounts, YAML/env configuration.

**Related docs:** [`data_scanning.md`](data_scanning.md) (PII scan reference),
[`../docker/README.md`](../docker/README.md) (container layout),
[`NER_PLUGGABLE_MODELS_DESIGN.md`](NER_PLUGGABLE_MODELS_DESIGN.md) (design notes).

---

## Table of contents

1. [Overview](#1-overview)
2. [Supported backends](#2-supported-backends)
3. [Download a model (offline prep)](#3-download-a-model-offline-prep)
4. [Model directory layout](#4-model-directory-layout)
5. [Optional manifest (`redibis-model.json`)](#5-optional-manifest-redibis-modeljson)
6. [Install on the host](#6-install-on-the-host)
7. [Upload via CLI](#7-upload-via-cli)
8. [Upload via REST API](#8-upload-via-rest-api)
9. [Activate & use in scans](#9-activate--use-in-scans)
10. [Docker & air-gapped deployments](#10-docker--air-gapped-deployments)
11. [Configuration reference](#11-configuration-reference)
12. [Upload security & validation](#12-upload-security--validation)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Overview

redibis ships **NER runtime code only** (`pip install redibis[ner-runtime]`):
Presidio pattern recognizers + the GLiNER library + PyTorch. **Weights are not
included** in the Docker image or the PyPI package.

| Component | Included in image/package? | Notes |
|-----------|----------------------------|-------|
| Presidio regex path | Yes | Works with no NER model |
| GLiNER library | Yes | Code only |
| GLiNER / HF weights | **No** | You download and mount them |
| HuggingFace hub access at scan time | **No** | `HF_HUB_OFFLINE=1` in images; `local_files_only=True` in code |

Without a local model path, PII scans still run on the **regex** engine; NER
column scoring is skipped with a logged notice.

```
┌──────────────────┐     download (once)      ┌─────────────────┐
│ HuggingFace /    │ ───────────────────────► │ host ./models/  │
│ your artifact    │                          │ or S3 / volume  │
└──────────────────┘                          └────────┬────────┘
                                                     │
              copy / mount / upload                  │
                                                     ▼
                                            ┌─────────────────┐
                                            │ REDIBIS_MODELS  │
                                            │ _DIR (/models)  │
                                            └────────┬────────┘
                                                     │
              activate (YAML / env / session)        │
                                                     ▼
                                            ┌─────────────────┐
                                            │ PII scan uses   │
                                            │ NERBackend      │
                                            └─────────────────┘
```

---

## 2. Supported backends

| Backend key | Status | Wraps | Notes |
|-------------|--------|-------|-------|
| `gliner` | **Implemented** | `GLiNER.from_pretrained(path, local_files_only=True)` | Default; zero-shot with custom label lists |
| `hf_token_cls` | Planned | HuggingFace token-classification checkpoints | e.g. domain fine-tunes |
| `spacy` | Planned | `spacy.load(path)` | Classic spaCy pipelines |
| `remote` | Planned | HTTP model server | Shared GPU inference |

Today, uploads and discovery accept **`gliner`** models. A directory is treated
as GLiNER when it contains `gliner_config.json` or `*.safetensors` weights.

---

## 3. Download a model (offline prep)

Download weights **before** deploying to an air-gapped or offline environment.
Do this on a machine with internet access, then copy or upload the resulting
folder.

### 3.1 Hugging Face CLI (recommended)

Install the Hub client once:

```bash
pip install "huggingface_hub[cli]"
```

**Shortcut (repo script):** download, write manifest, and zip in one step:

```bash
./scripts/download_ner_model.sh
# → ./models/gliner-multi-v2.1/  and  ./models/gliner-multi-v2.1.zip
```

Download a public GLiNER checkpoint into a local folder:

```bash
MODEL_ID="urchade/gliner_multi-v2.1"
DEST="./models/gliner-multi-v2.1"

huggingface-cli download "$MODEL_ID" --local-dir "$DEST"
```

For a **private** or gated model, authenticate first:

```bash
huggingface-cli login
# or: export HF_TOKEN=hf_...
huggingface-cli download your-org/gliner-arabic-pii-v2 --local-dir ./models/gliner-ar-v2
```

Equivalent using the Python API:

```python
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="urchade/gliner_multi-v2.1",
    local_dir="./models/gliner-multi-v2.1",
    local_dir_use_symlinks=False,
)
```

### 3.2 Verify the download

A usable GLiNER folder must include at least:

- `gliner_config.json`
- Weight files: `model.safetensors` and/or `pytorch_model.bin`

Quick check:

```bash
ls ./models/gliner-multi-v2.1/
# gliner_config.json  model.safetensors  (and/or pytorch_model.bin)
```

Optional smoke test (requires `gliner` installed):

```python
from gliner import GLiNER

model = GLiNER.from_pretrained("./models/gliner-multi-v2.1", local_files_only=True)
print(model.predict_entities("Alice Smith lives in Cairo", ["person", "address"]))
```

### 3.3 Custom / fine-tuned GLiNER

Fine-tuned checkpoints use the **same on-disk layout** as upstream GLiNER. If
you trained with the GLiNER toolkit or exported to Hugging Face, download or
copy the folder the same way. Add a `redibis-model.json` manifest (see §5) when
your label set differs from the defaults.

### 3.4 Other NER formats (not yet supported as upload targets)

Classic Hugging Face token-classification models (`config.json`,
`pytorch_model.bin`, label maps) and spaCy pipelines (`meta.json`, `ner/`) are
on the roadmap (`hf_token_cls`, `spacy` backends). Until those backends ship,
convert or export to GLiNER-compatible weights, or mount the path directly via
`pii.ner.model_path` only if you have a custom adapter — the stock registry
accepts **`gliner`** only.

---

## 4. Model directory layout

Each model lives in its **own subdirectory** under the models root
(`REDIBIS_MODELS_DIR`, default `/models`):

```
/models/                          ← models root (REDIBIS_MODELS_DIR)
├── gliner-multi-v2.1/
│   ├── gliner_config.json
│   ├── model.safetensors
│   └── redibis-model.json        ← optional manifest
├── gliner-ar-v2/
│   ├── gliner_config.json
│   ├── model.safetensors
│   └── redibis-model.json
└── _quarantine/                  ← temporary; created during upload (ignore)
```

**Default labels** (used when no manifest and no YAML override):

`person`, `phone number`, `email`, `address`, `national id`, `passport`,
`credit card`

`organization` is intentionally excluded from the defaults — a company name
is not personal data (see `redibis.models.NON_PII_ENTITIES`). Add it to
`pii.ner.labels` explicitly if you need organization evidence for other
purposes; detected `ORGANIZATION` columns still classify as `internal` and
carry no PII tags or masking.

Override labels in the manifest or in `pii.ner.labels` when your model targets
a different entity set (e.g. Arabic PII types).

---

## 5. Optional manifest (`redibis-model.json`)

Place this file **inside** the model directory. redibis reads it for discovery,
upload validation, and default thresholds.

```json
{
  "type": "gliner",
  "name": "gliner-ar-v2",
  "labels": [
    "person",
    "phone number",
    "national id",
    "address",
    "iban",
    "email"
  ],
  "language": ["ar", "en"],
  "default_threshold": 0.35
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | No | Backend key; default inferred as `gliner` |
| `name` | No | Display name; defaults to directory name |
| `labels` | No | Entity strings passed to GLiNER; defaults to built-in list |
| `language` | No | Informational; not enforced by the engine |
| `default_threshold` | No | Score floor for this model (overrides `pii.ner.threshold` when loading) |

If the manifest is missing, redibis infers `type: gliner` from
`gliner_config.json` and uses default labels.

---

## 6. Install on the host

Three equivalent ways to get weights onto the machine that runs redibis.

### 6.1 Copy or bind-mount (simplest)

```bash
# After huggingface-cli download (§3.1)
mkdir -p /srv/ner-models
cp -a ./models/gliner-multi-v2.1 /srv/ner-models/

export REDIBIS_MODELS_DIR=/srv/ner-models
export REDIBIS_NER_MODEL=/srv/ner-models/gliner-multi-v2.1
```

Docker Compose (local stack) mounts `./models` → `/models` by default:

```bash
mkdir -p ./models/gliner-multi-v2.1
cp -a /path/from/download/* ./models/gliner-multi-v2.1/
REDIBIS_NER_MODEL=/models/gliner-multi-v2.1 ./docker/scripts/run-local.sh --build
```

### 6.2 Package as an archive for upload

Create a **zip** or **tar.gz** with a single top-level folder:

```bash
cd ./models
zip -r gliner-multi-v2.1.zip gliner-multi-v2.1/
# or
tar -czf gliner-multi-v2.1.tar.gz gliner-multi-v2.1/
```

Archive layout:

```
gliner-multi-v2.1.zip
└── gliner-multi-v2.1/
    ├── gliner_config.json
    ├── model.safetensors
    └── redibis-model.json
```

If the archive unpacks to a **single** subdirectory, redibis treats that folder
as the model root. Flat archives (files at the top level) are also accepted.

Then upload (§7) or copy the archive to the target host and run
`redibis models upload`.

### 6.3 Object storage / init container

In Kubernetes or cloud runtimes, populate `/models` from S3, GCS, or an init
container that runs `huggingface-cli download`, then set `REDIBIS_NER_MODEL` on
the workload. No redibis-specific upload step is required if the volume is
already mounted read-only.

---

## 7. Upload via CLI

Requires `redibis[ner-runtime]` (or full install). Upload runs **quarantine →
validate → smoke inference → promote** (see §12).

```bash
# List models under REDIBIS_MODELS_DIR (default /models)
redibis models list
redibis models list --json

# Upload archive; optional --name overrides folder name
redibis models upload ./gliner-multi-v2.1.zip --name gliner-multi-v2.1

# Use a custom models root from YAML
redibis models upload ./model.zip --config redibis.yaml

# Or explicit directory
redibis models upload ./model.zip --models-dir /data/team-a/ner-models --name my-ner

# Activate: writes pii.ner.model_path into redibis.yaml when --config is given
redibis models activate gliner-multi-v2.1 --config redibis.yaml

# Remove a model
redibis models delete gliner-multi-v2.1 --yes
```

**Models directory resolution** (first match wins):

1. `--models-dir` CLI flag
2. `pii.models_dir` in `--config redibis.yaml`
3. `REDIBIS_MODELS_DIR` environment variable
4. `/models`

Successful upload prints:

```text
ok: gliner-multi-v2.1 → /models/gliner-multi-v2.1
```

---

## 8. Upload via REST API

Available when the FastAPI dashboard is running (`pip install redibis[web]`).
Auth is on by default — sign in first
([`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl)). Upload is
**admin**-only (explorer is read-only).

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/models?session_id=…` | List models; marks session-active path |
| `POST` | `/api/models/upload` | Upload `.zip` / `.tar.gz` / `.tgz` |
| `DELETE` | `/api/models/{name}` | Delete model directory |
| `POST` | `/api/sessions/{id}/models/activate` | Set session NER model |

### Upload (multipart form)

```bash
curl -s -b "$RB_COOKIES" -X POST http://localhost:8000/api/models/upload \
  -H "X-CSRF-Token: $CSRF" \
  -F "file=@gliner-multi-v2.1.zip" \
  -F "name=gliner-multi-v2.1"
```

Optional form fields:

- `session_id` — use that session's `pii_models_dir` instead of the global default
- `models_dir` — explicit models root for this request

Response (success):

```json
{
  "status": "ok",
  "name": "gliner-multi-v2.1",
  "path": "/models/gliner-multi-v2.1",
  "spec": {
    "type": "gliner",
    "name": "gliner-multi-v2.1",
    "labels": ["person", "email"],
    "path": "/models/gliner-multi-v2.1"
  },
  "warnings": []
}
```

### Activate in a session

```bash
curl -s -b "$RB_COOKIES" -X POST "http://localhost:8000/api/sessions/${SESSION_ID}/models/activate" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"name": "gliner-multi-v2.1"}'
```

Session activation updates **in-memory session config only**. To persist across
restarts, write `pii.ner.model_path` into `redibis.yaml` or set
`REDIBIS_NER_MODEL` in the environment.

Web UI: **Settings → NER Models** (when enabled in your deployment).

---

## 9. Activate & use in scans

After the model is on disk, point redibis at it.

### 9.1 YAML (`redibis.yaml`)

```yaml
pii:
  models_dir: /models
  engines: both          # regex | ner | both
  ner:
    type: gliner
    model_path: /models/gliner-multi-v2.1
    labels: []           # optional; manifest wins when present
    device: cpu
    threshold: 0.3
    batch_size: 8
```

### 9.2 Environment (Docker / K8s friendly)

```bash
export REDIBIS_MODELS_DIR=/models
export REDIBIS_NER_MODEL=/models/gliner-multi-v2.1
```

Resolution order for the active path: `pii.ner.model_path` → `REDIBIS_NER_MODEL`
→ deprecated `pii.gliner.model_id` (local cache only).

### 9.3 One-off scan flags

```bash
redibis scan customers.csv telecom.customers \
  --mode pii \
  --ner-model /models/gliner-multi-v2.1 \
  --pii-engines both
```

(`--gliner-model` is an alias for `--ner-model`.)

### 9.4 Python

```python
from redibis import PIIScan
from redibis.services.scan_service import ScanConfig

cfg = ScanConfig(
    table="telecom.customers",
    run_pii=True,
    pii_gliner_model="/models/gliner-multi-v2.1",
)
result = PIIScan(cfg).run(df)
```

Detection results expose `gliner_score`, `gliner_label`, and `ner_engine` on each
`PIIDetection` (field names kept for contract compatibility).

---

## 10. Docker & air-gapped deployments

Production and local Docker images:

- Do **not** bake NER weights
- Set `HF_HUB_OFFLINE=1` so nothing contacts Hugging Face during scans
- Declare `VOLUME /models`
- Mount host weights read-only

```bash
docker run -v /srv/ner-models:/models:ro \
  -e REDIBIS_NER_MODEL=/models/gliner-multi-v2.1 \
  redibis scan /data/customers.csv --table telecom.customers --mode pii
```

**Air-gap workflow**

1. On a connected machine: `huggingface-cli download …` (§3.1)
2. Transfer the folder or zip (sneakernet, internal artifact repo)
3. Mount or `redibis models upload` on the isolated host
4. Set `REDIBIS_NER_MODEL` — scans never need outbound network

Regex-only PII works with **no** model mount; NER is optional.

---

## 11. Configuration reference

| Setting | Default | Description |
|---------|---------|-------------|
| `pii.models_dir` | `/models` | Root for discovery and uploads |
| `pii.ner.model_path` | `""` | Active model directory |
| `pii.ner.type` | `gliner` | Backend key |
| `pii.ner.labels` | `[]` | Override manifest labels when non-empty |
| `pii.ner.threshold` | `0.3` | Entity score floor |
| `pii.ner.device` | `cpu` | `cpu` or CUDA device |
| `pii.ner.batch_size` | `8` | Batch size for column value scoring |
| `pii.ner.always_run` | `false` | Run NER even when regex confidence is high |
| `REDIBIS_MODELS_DIR` | — | Env override for models root |
| `REDIBIS_NER_MODEL` | — | Env override for active model path |

Per-team isolation: give each team its own `pii.models_dir` or
`REDIBIS_MODELS_DIR`; session `pii_models_dir` overrides for web sessions.

---

## 12. Upload security & validation

Uploads are treated as **untrusted**. Before promotion to `{models_dir}/{name}/`:

| Check | Behavior |
|-------|----------|
| Zip-slip | Reject paths with `..` or absolute members |
| Size cap | 5 GB maximum upload |
| Decompression bomb | Extracted size budget ≈ `min(5 GB, 4× archive size)` |
| Disk space | Require free space before extract |
| Pickle files | **Reject** `*.pkl` / `*.pickle` |
| Legacy `.bin` only | Warn if no `.safetensors` present |
| Manifest / layout | `NERModelRegistry.validate()` — must load |
| Smoke inference | Three short strings scored through the backend |

Failed uploads are removed from `_quarantine/`; nothing is partially promoted.

---

## 13. Troubleshooting

Operator runbook (dashboard reload, log grep, verify NER vs regex-only):
[`WEB_ADMIN.md` — Development: code reload and NER verification](WEB_ADMIN.md#development-code-reload-and-ner-verification).

### `NER model not found at '…'. Mount model weights under /models or set REDIBIS_NER_MODEL.`

The path in `pii.ner.model_path` or `REDIBIS_NER_MODEL` does not exist inside
the container/process, or files are incomplete. Verify mount, copy, or upload;
ensure `gliner_config.json` and weight files are present.

### Scan runs regex-only; no NER scores

- No model path configured — **Activate** the model in Settings → NER Models (or set `REDIBIS_NER_MODEL=/models/…`)
- `pii.engines: regex` — set to `both` or `ner`
- `gliner` not installed → `pip install redibis[ner-runtime]`
- Model failed to load — check logs at `DEBUG`

### NER loads but all `gliner_score` values are null (Docker / offline)

Docker sets `HF_HUB_OFFLINE=1`. GLiNER weights alone are not enough — the base
tokenizer (e.g. `microsoft/mdeberta-v3-base` for multi-v2.1) must be bundled
offline:

```bash
./scripts/download_ner_model.sh --tokenizer-only --name gliner-multi-v2.1
```

This creates `models/gliner-multi-v2.1/encoder/` beside the weights. Restart the
stack, then **Activate** the model in the dashboard before scanning.

Fresh downloads bundle `encoder/` automatically (default since the download script
was updated).

### Upload rejected: pickle / unsafe archive

Re-export weights as **safetensors**. Do not upload pickle checkpoints.

### Upload rejected: smoke inference failed

Weights load but inference errored. Test locally with §3.2; confirm PyTorch and
`gliner` versions match the training environment (`requirements/constraints.txt`
in this repo pins versions for Docker).

### Wrong labels / missed entities

Add or fix `redibis-model.json` labels to match what the model was trained or
zero-shot tuned for. GLiNER is sensitive to label wording — use the same strings
as in training docs.

### Hugging Face download works locally but not in Docker

Expected: images are offline. Download on the host, mount into `/models`, set
`REDIBIS_NER_MODEL=/models/your-model`.

### Session activated a model but CLI scans ignore it

Session activation does not change global YAML. Persist with:

```bash
redibis models activate my-ner --config redibis.yaml
```

or export `REDIBIS_NER_MODEL` before scanning.

---

## Quick reference

```bash
# 1. Download (internet-connected machine; bundles encoder/ for offline Docker)
./scripts/download_ner_model.sh
# Existing weights missing encoder/? (fixes null NER scores in Docker):
./scripts/download_ner_model.sh --tokenizer-only --name gliner-multi-v2.1

# 2. Optional manifest — written automatically by the script; edit labels if needed

# 3. Install — pick one:
#    (folder already under ./models/ after the script)
redibis models activate gliner-multi-v2.1 --config redibis.yaml
# or upload elsewhere:
redibis models upload ./models/gliner-multi-v2.1.zip --name gliner-multi-v2.1

# 4. Scan
redibis scan data.csv telecom.customers --config redibis.yaml --mode pii
```
