# ============================================================
# redibis — embedded Postgres + pgvector for column memory
# Base: official pgvector image (PostgreSQL 16 + vector extension)
# ============================================================
FROM pgvector/pgvector:pg16

LABEL maintainer="redibis"
LABEL description="Embedded Postgres + pgvector for redibis column memory store"

COPY docker/postgres/init/ /docker-entrypoint-initdb.d/
