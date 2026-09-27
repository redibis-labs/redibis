#!/bin/bash
# ============================================================
# redibis — Local Development Entrypoint
# Starts both JupyterLab (8888) and FastAPI dashboard (8000)
# ============================================================
set -e

echo "════════════════════════════════════════════"
echo "  redibis — easily govern"
echo "════════════════════════════════════════════"
echo "  JupyterLab:    http://localhost:${EXTERNAL_JUPYTER_PORT:-8888}"
echo "  Dashboard:     http://localhost:${EXTERNAL_API_PORT:-8000}"
echo "  API Docs:      http://localhost:${EXTERNAL_API_PORT:-8000}/docs"
echo "════════════════════════════════════════════"

# Start FastAPI webapp in background.
# Output is teed to /tmp/uvicorn.log so it shows in `docker logs` AND can be
# inspected directly with `docker exec redibis-lab tail -f /tmp/uvicorn.log`.
echo "[entrypoint] Starting FastAPI dashboard on :8000..."
( micromamba run -n redibis \
    uvicorn redibis.webapp.backend:app \
      --host 0.0.0.0 --port 8000 \
      --log-level info \
      --reload --reload-dir /tmp/redibis_pkg/redibis 2>&1 | tee /tmp/uvicorn.log ) &
UVICORN_PID=$!

# Start JupyterLab in foreground (main process)
echo "[entrypoint] Starting JupyterLab on :8888..."
exec micromamba run -n redibis \
  jupyter lab \
    --ip=0.0.0.0 \
    --config=/etc/jupyter/jupyter_notebook_config.py