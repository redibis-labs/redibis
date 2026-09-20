# PII Detection Suite — Docker Deployment Guide

> ⚠️ **Legacy / optional — Docker is no longer the default.** The supported default is bare-metal
> (and air-gapped bare-metal): see
> [`../docs/CODEGEN_SERVICE.md`](../docs/CODEGEN_SERVICE.md) (local codegen; airgap deploy is a separate commercial product).
> This guide is kept for teams that still want containers.

This folder contains all Docker and deployment artefacts for running the PII Detection Suite across three environments.

---

## Folder Structure

```
docker/
├── local/                        ← Developer laptop / WSL / Docker Desktop
│   ├── Dockerfile                 Full stack: JupyterLab + FastAPI + MinIO
│   ├── environment.yml            Conda env with all deps (inc. JupyterLab)
│   ├── constraints.txt            Version pins to prevent solver backtracking
│   ├── jupyter_notebook_config.py Jupyter server config (no-token local mode)
│   └── docker-compose.yml         Orchestrates pii-lab + minio + minio-init
│
├── gcp/                          ← Google Cloud Run / GKE
│   ├── Dockerfile                 Headless API image (no Jupyter)
│   ├── environment.yml            Conda env with GCP-specific extras
│   ├── constraints.txt            Version pins
│   ├── cloudbuild.yaml            Cloud Build CI/CD pipeline definition
│   └── k8s-deployment.yaml        GKE Deployment + Service + HPA manifests
│
├── azure/                        ← Azure Container Apps / AKS
│   ├── Dockerfile                 Headless API image with Azure SDK extras
│   ├── environment.yml            Conda env with azure-storage-blob / adlfs
│   ├── constraints.txt            Version pins
│   ├── azure-pipelines.yml        Azure DevOps CI/CD pipeline definition
│   ├── aci-deploy.json            ARM template for Azure Container Instances
│   └── aks-deployment.yaml        AKS Deployment + Service + HPA manifests
│
└── scripts/                      ← Convenience shell scripts
    ├── run-local.sh               Build and start the full local stack
    ├── deploy-gcp.sh              Build locally, push to Artifact Registry, deploy Cloud Run
    ├── deploy-cloudbuild.sh       Offload build to Cloud Build, deploy Cloud Run
    ├── deploy-gke.sh              Create/update GKE Autopilot cluster + apply manifests
    ├── deploy-azure.sh            Build locally, push to ACR, deploy Container Apps
    └── deploy-aks.sh              Create/update AKS cluster, attach ACR, apply manifests
```

Vendor-hosted codegen Docker/scripts live under
a vendor-internal tree (not shipped in the OSS export).

---

## Quick Start

### Local (Docker Compose)
```bash
# Start full stack (JupyterLab + MinIO)
./docker/scripts/run-local.sh

# Force full dependency rebuild
./docker/scripts/run-local.sh --no-cache

# Include OpenMetadata catalog stack (~4–6 GiB RAM extra)
./docker/scripts/run-local.sh --build --with-openmetadata

# Stop and remove all containers + volumes
./docker/scripts/run-local.sh --stop
```

| Service        | URL                                 |
|----------------|-------------------------------------|
| JupyterLab     | http://localhost:8889               |
| FastAPI UI     | http://localhost:8080 (login; [`DASHBOARD_AUTH.md`](../docs/DASHBOARD_AUTH.md)) |
| MinIO Console  | http://localhost:9001 (minioadmin)  |
| OpenMetadata   | http://localhost:8585 (`up --with-openmetadata`) |

**Logs:** `docker logs -f <container>` or `docker exec <container> tail -f /tmp/uvicorn.log`. Full operator runbook (session flush, LLM debug API, host deploy log path): [`docs/WEB_ADMIN.md`](../docs/WEB_ADMIN.md#fetching-logs).

---

### GCP — Codegen service (vendor-internal)

Hosted codegen deploy scripts moved to
vendor-internal deploy scripts (not shipped in the OSS export).
See [`docs/CODEGEN_SERVICE.md`](../docs/CODEGEN_SERVICE.md) and
your vendor onboarding docs.

```bash
# vendor CI only
# vendor-internal codegen deploy (not shipped in OSS) YOUR_PROJECT_ID us-central1
```

Point on-prem `agents.codegen_service_url` + `REDIBIS_CODEGEN_TOKEN` at the
vendor gateway when using remote mode.

---

### GCP — Cloud Run (local push)
```bash
./docker/scripts/deploy-gcp.sh YOUR_PROJECT_ID us-central1
```

### GCP — Cloud Build CI (remote build)
```bash
./docker/scripts/deploy-cloudbuild.sh YOUR_PROJECT_ID us-central1
```

### GCP — GKE Autopilot
```bash
./docker/scripts/deploy-gke.sh YOUR_PROJECT_ID us-central1
```

---

### Azure — Container Apps (local push)
```bash
./docker/scripts/deploy-azure.sh pii-detection-rg myregistry eastus
```

### Azure — AKS
```bash
./docker/scripts/deploy-aks.sh pii-detection-rg myregistry eastus
```

### Azure — Container Instances (ACI via ARM)
```bash
az deployment group create \
  --resource-group pii-detection-rg \
  --template-file docker/azure/aci-deploy.json \
  --parameters \
      acrLoginServer=myregistry.azurecr.io \
      acrUsername=<sp-client-id> \
      acrPassword=<sp-secret> \
      storageAccountName=<storage-account> \
      storageAccountKey=<key>
```

---

## Design Principles (from skill file)

All Dockerfiles follow the layering strategy defined in `docker_lab_creation.md`:

| Layer | Content | Cache invalidated when |
|-------|---------|------------------------|
| 1 | `apt-get` system packages | OS requirements change |
| 2 | Conda / Pip environment | `environment.yml` or `constraints.txt` changes |
| 3 | App code & config | Source code changes |

**BuildKit cache mounts** are used on Layers 2 to avoid re-downloading gigabytes of packages on every rebuild:
```dockerfile
RUN --mount=type=cache,target=/opt/conda/pkgs \
    --mount=type=cache,target=/root/.cache/pip \
    micromamba create -y -n pii-detection -f environment.yml
```

---

## Storage Configuration

All three environments read the same environment variables for storage:

| Variable | Description | Local Default |
|----------|-------------|---------------|
| `S3_ENDPOINT` | MinIO / SeaweedFS endpoint | `http://minio:9000` |
| `AWS_ACCESS_KEY_ID` | S3 access key | `minioadmin` |
| `AWS_SECRET_ACCESS_KEY` | S3 secret | `minioadmin` |
| `PII_STORE_BUCKET` | Contract storage bucket | `pii-contracts` |
| `PII_STORE_DIR` | Local fallback dir | `/tmp/kimi_store` |

For Azure, set `AZURE_STORAGE_ACCOUNT` and `AZURE_STORAGE_KEY` instead of S3 variables.

---

## NER models (bring your own weights)

**Local dev** bind-mounts `./models` → `/models`. **Prod / airgap** images keep `/models`
as a volume — populate on the host (`scripts/download_ner_model.sh`) or via Compose
`${REDIBIS_MODELS_DIR:-./models}:/models`. Weights are **not** baked into published images
(keeps Docker Hub builds small). The regex (Presidio pattern) path works out of the box;
NER scoring needs weights mounted at `/models`.

| Variable | Purpose | Example |
|----------|---------|---------|
| `REDIBIS_NER_MODEL` | Active GLiNER model directory | `/models/gliner-ar-v1` |
| `REDIBIS_MODELS_DIR` | Host path mounted to `/models` (Compose) | `./models` |
| `HF_HUB_OFFLINE` | Set to `1` in images — no hub downloads | (automatic) |

Local Compose mounts `./models` → `/models` **read-write** so the dashboard can
upload archives. Production deployments that mount `:ro` should pre-populate weights
on the host/volume or use `redibis models upload` from outside the container.

```bash
# Local stack with a model directory on the host
mkdir -p ./models/gliner-ar-v1   # copy GLiNER weights here
REDIBIS_NER_MODEL=/models/gliner-ar-v1 ./docker/scripts/run-local.sh --build
```

Cloud Run / AKS / GKE: attach a volume or init container that populates `/models`, then
set `REDIBIS_NER_MODEL` on the deployment.

---

## Troubleshooting

**WSL Docker credential error** (`exec format error with docker-credential-desktop.exe`):
The deploy scripts automatically create a temporary clean `DOCKER_CONFIG` to bypass this. If you see it outside the scripts, run:
```bash
cat ~/.docker/config.json | python3 -c "import json,sys; d=json.load(sys.stdin); d.pop('credsStore',None); print(json.dumps(d))" > /tmp/dc.json && mv /tmp/dc.json ~/.docker/config.json
```

**Conda `PackagesNotFoundError`**: The `constraints.txt` and wildcard pins (e.g. `sqlalchemy=1.4.*`) prevent this. If it occurs, move the conflicting package to the `pip:` section in `environment.yml`.

**Transient network failures during `docker build`**: BuildKit cache mounts are in place — simply re-run `docker build`. The resolver will resume from the cache.
