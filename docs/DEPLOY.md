# redibis — Build, Run & Deploy Guide

> ⚠️ **Legacy / optional — Docker is no longer the default.** Bare-metal (and air-gapped
> bare-metal) is the supported default path under the **enterprise** workspace: see
> [`CODEGEN_SERVICE.md`](CODEGEN_SERVICE.md) and
> [`CODEGEN_SERVICE.md`](CODEGEN_SERVICE.md).
> This Docker/Podman guide is kept for teams that still want containers.

How to build the image, run the full stack locally with hot-reload, and package
everything for an air-gapped (offline) host.

There are **two** independent Docker setups in this repo:

| Setup | Compose file | Image | Purpose |
|-------|--------------|-------|---------|
| **Local dev** | `docker/local/docker-compose.yml` | `redibis:local` | Laptop / WSL. Bind-mounts your source for instant edits. |
| **Airgap / prod** | `docker-compose.yml` (repo root) | `localhost/redibis:latest` | Self-contained image, no source mount, runs from pre-loaded tarballs. |

Both serve the same things:

- **JupyterLab** → `:8888`
- **FastAPI dashboard + REST API** → `:8000` (`/docs` for Swagger; requires a session)
- **MinIO** S3 API → `:9000`, Web Console → `:9001` (`minioadmin` / `minioadmin`)

---

## 1. Prerequisites

- **Local dev:** Docker (Docker Desktop or engine) + `docker compose`. On WSL,
  the scripts auto-patch the credential-helper bug.
- **Air-gapped host:** `podman` + `podman-compose` (the import script uses Podman).
- For hot-reload watch mode: `inotify-tools` (Linux/WSL) or `fswatch` (macOS).

---

## 2. Run locally (recommended for development)

From the repo root:

```bash
# First run (or after changing the Dockerfile / compose / entrypoint):
./docker/scripts/run-local.sh --build

# Fast start (reuse existing image):
./docker/scripts/run-local.sh

# Full clean rebuild (no cache):
./docker/scripts/run-local.sh --no-cache
```

Then open:

- Dashboard:     http://localhost:8080
- API docs:      http://localhost:8080/docs
- JupyterLab:    http://localhost:8889
- MinIO Console: http://localhost:9001

> Note: locally, the dashboard/API is published on **:8080** and JupyterLab on
> **:8889** (see `docker/local/docker-compose.yml`).

### Editing code & seeing changes

The local stack bind-mounts your repo **and** overlays the live `redibis/`
package onto the path Python actually imports from, so:

- **Templates / CSS / JS** (`redibis/webapp/templates/*.html`,
  `redibis/webapp/static/app.js`, `app.css`) → just save and **hard-refresh**
  the browser (`Ctrl+Shift+R` / `Cmd+Shift+R`). No restart needed.
- **Python** (`*.py`) → uvicorn runs with `--reload`, so it restarts automatically.

> The dashboard root (`/`) renders `templates/index.html`, which loads
> `static/app.js` + `static/app.css`. Those three files **are** the UI — edit
> them directly.

### Hot-deploy / watch (for non-mounted containers)

If you ever run against a container that does **not** mount the source:

```bash
./docker/scripts/run-local.sh --deploy   # push changed UI + py files once
./docker/scripts/run-local.sh --watch    # auto-push on every save
./docker/scripts/run-local.sh --logs      # tail logs
./docker/scripts/run-local.sh --stop      # stop + remove volumes
```

`deploy/deploy.sh` syncs the **entire** package (auto-detecting bind-mount vs
container vs host) and restarts uvicorn; `docker/scripts/deploy-changes.sh`
pushes the individual served files.

---

## 3. Build the production image only

```bash
docker build -t localhost/redibis:latest .
```

This uses the root `Dockerfile`: it installs the package into the base conda
env and starts both the dashboard and JupyterLab via `deploy/entrypoint.sh`.

Run it standalone (with MinIO + bucket init):

```bash
docker compose up -d        # uses the root docker-compose.yml
# Dashboard: http://localhost:8000   JupyterLab: http://localhost:8888
docker compose down -v      # stop + wipe storage
```

---

## 4. Export for an air-gapped environment

On a machine **with** internet/registry access:

```bash
./deploy/export_airgap_images.sh
```

This will:

1. Build `localhost/redibis:latest` from the root `Dockerfile`.
2. Pull `minio` and `mc` images.
3. Save all three to tarballs and copy the run files into **`airgap_bundle/`**:

```
airgap_bundle/
├── redibis_image.tar
├── minio_image.tar
├── mc_image.tar
├── docker-compose.yml
└── import_and_run.sh
```

Transfer the whole `airgap_bundle/` directory to the offline host (USB, scp to
a jump box, etc.). It is already git-ignored and excluded from the Docker build
context.

### On the air-gapped host

```bash
cd airgap_bundle
./import_and_run.sh
```

This loads the three images into Podman and runs `podman-compose up -d`.
The `minio-init` service waits for MinIO and creates the required buckets
(`pii-contracts`, `pii-reports`, `pii-configs`, `quality-configs`).

Open:

- Dashboard:     http://localhost:8000  (login required; [`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md))
- JupyterLab:    http://localhost:8888
- MinIO Console: http://localhost:9001

To stop: `podman-compose down`  (add `-v` to also remove the MinIO volume).

---

## 5. Storage configuration (env vars)

The backend reads these (set in both compose files):

| Variable | Default | Meaning |
|----------|---------|---------|
| `S3_ENDPOINT_URL` | _(unset → AWS)_ | S3/MinIO endpoint, e.g. `http://minio:9000` |
| `S3_ACCESS_KEY` / `AWS_ACCESS_KEY_ID` | — | Access key |
| `S3_SECRET_KEY` / `AWS_SECRET_ACCESS_KEY` | — | Secret key |
| `S3_REGION` / `AWS_DEFAULT_REGION` | `us-east-1` | Region |
| `S3_USE_SSL` | `false` | `true` for HTTPS endpoints |
| `S3_CONTRACTS_BUCKET` | `pii-contracts` | ODCS contract store |
| `S3_RUNS_BUCKET` | `pii-reports` | Per-run artifacts |
| `S3_PII_CONFIGS_BUCKET` | `pii-configs` | PII config store |
| `S3_QUALITY_CONFIGS_BUCKET` | `quality-configs` | Quality config store |
| `USE_LOCAL_STORAGE` | `false` | `true` → use local filesystem instead of S3 |

> If neither `S3_ENDPOINT_URL` nor an access key is set, the backend silently
> falls back to **local filesystem** storage (`./_local_storage`). If your
> dashboard isn't writing to MinIO, this is almost always the cause.

---

## 6. Troubleshooting

**"I edited an HTML/CSS/JS file and nothing changed."**
- Make sure you edited a *served* file: `webapp/templates/*.html` or
  `webapp/static/app.{js,css}`.
- Hard-refresh the browser (`Ctrl+Shift+R`) to bypass the browser cache.
- Local stack: confirm it was started with the current compose (rebuild once
  with `--build` after pulling these changes).
- Air-gapped/prod: you must **rebuild and re-export** the image — the prod image
  bakes the source in, it is not live-mounted.

**Dashboard can't reach storage / data not persisting.**
- Check `S3_ENDPOINT_URL` is set and points at the `minio` service.
- Confirm buckets exist (the `minio-init` service creates them; check its logs).

**uvicorn didn't come up.**
```bash
# Prod (root compose) — dashboard logs go to the container stdout:
docker logs -f redibis

# Local stack — full container output (Jupyter + uvicorn):
docker logs -f redibis-lab
# ...or just the dashboard log:
docker exec redibis-lab tail -40 /tmp/uvicorn.log
```
> The `/tmp/uvicorn.log` file only exists once the container has (re)started the
> dashboard. If it's missing, the container probably hasn't been rebuilt since
> this change — use `docker logs redibis-lab` instead, or rebuild with
> `./docker/scripts/run-local.sh --build`.
