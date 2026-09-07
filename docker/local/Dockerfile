# ============================================================
# redibis — Local Development Image
# Target: Developer laptop / WSL / Docker Desktop
# Base:   mambaorg/micromamba (fastest Conda solver)
#
# Serves:
#   :8888  JupyterLab (main process)
#   :8000  FastAPI dashboard + REST API
# ============================================================
FROM mambaorg/micromamba:1.5.8

LABEL maintainer="redibis"
LABEL description="redibis — data contract pipeline with quality + PII detection"
LABEL version="1.0.0"
LABEL environment="local"

# --- Layer 1: System packages ---
USER root

ENV CONDA_ENV_NAME=redibis
ENV PYTHONUNBUFFERED=1
ENV JUPYTER_ALLOW_INSECURE_WRITES=true
ENV PIP_NO_CACHE_DIR=0
# NER: code in image, weights mounted at runtime (see README BYOM section)
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
ENV REDIBIS_NER_MODEL=""

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    git-lfs \
    build-essential \
    gcc \
    g++ \
    make \
    cmake \
    default-jdk-headless \
    libgomp1 \
    libmagic1 \
    poppler-utils \
    wget \
    nano \
    vim \
    procps \
    htop \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /home/jovyan

# --- Layer 2: Python / Conda environment ---
COPY --chown=$MAMBA_USER:$MAMBA_USER docker/local/environment.yml docker/local/constraints.txt ./

RUN --mount=type=cache,target=/opt/conda/pkgs \
    --mount=type=cache,target=/root/.cache/pip \
    PIP_CONSTRAINT=constraints.txt \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=5 \
    micromamba create -y -n ${CONDA_ENV_NAME} -f environment.yml && \
    micromamba clean --all --yes



SHELL ["micromamba", "run", "-n", "redibis", "/bin/bash", "-c"]

# --- Layer 3: Configuration and application code ---
COPY --chown=$MAMBA_USER:$MAMBA_USER docker/local/jupyter_notebook_config.py /etc/jupyter/

# Install redibis (--no-deps: pip deps including redibis[mask] are in environment.yml)
COPY --chown=$MAMBA_USER:$MAMBA_USER pyproject.toml /tmp/redibis_pkg/pyproject.toml
COPY --chown=$MAMBA_USER:$MAMBA_USER redibis /tmp/redibis_pkg/redibis
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps -e "/tmp/redibis_pkg" && \
    python -c "from redibis.masking import transforms as T; assert T._HAS_FF3 and T._HAS_AESGCM, 'redibis[mask] deps missing — check environment.yml'"

# Column memory embeddings (memory-docker.yaml) — cache model at build time; runtime is offline.
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# --- Layer 4: Entrypoint ---
COPY --chown=$MAMBA_USER:$MAMBA_USER deployment_scripts/entrypoint.local.sh /usr/local/bin/entrypoint.sh
COPY --chown=$MAMBA_USER:$MAMBA_USER deployment_scripts/memory-init.sh /usr/local/bin/memory-init.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/memory-init.sh


EXPOSE 8888
EXPOSE 8000

VOLUME ["/models"]

WORKDIR /home/jovyan/work

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8888/api && curl -f http://localhost:8000/health || exit 1

USER $MAMBA_USER

CMD ["/usr/local/bin/entrypoint.sh"]
