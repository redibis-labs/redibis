# ============================================================
# redibis — PII Guard service (open-core)
# Stateless scan + de-identify; pack-driven RuleSet + policies
# ============================================================
FROM python:3.12-slim-bookworm

LABEL maintainer="redibis"
LABEL description="redibis PII Guard service"
LABEL service="redibis-pii-guard"

ENV PYTHONUNBUFFERED=1
ENV PORT=8090

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build context = repository root
COPY pyproject.toml README.md ./
COPY redibis ./redibis
COPY services/pii_guard/pyproject.toml ./services/pii_guard/pyproject.toml
COPY services/pii_guard/README.md ./services/pii_guard/README.md
COPY services/pii_guard/redibis_pii_guard ./services/pii_guard/redibis_pii_guard

RUN pip install --no-cache-dir \
    fastapi "uvicorn[standard]" httpx pydantic pyyaml pandas \
    open-data-contract-standard phonenumbers && \
    pip install --no-cache-dir --no-deps -e . && \
    pip install --no-cache-dir --no-deps -e ./services/pii_guard

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f "http://localhost:${PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn redibis_pii_guard.app:app --host 0.0.0.0 --port ${PORT}"]
