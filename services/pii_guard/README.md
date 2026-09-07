# Redibis PII Guard
#
# Stateless FastAPI service composing library `ScanBackend` + `MaskBackend`.
# Scan and mask route modules are separate so they can later become
# `pii-scan` / `pii-mask` services with this guard as a thin composer.

## Run locally

```bash
# from repo root
pip install -e .
pip install -e ./services/pii_guard

export REDIBIS_PII_GUARD_API_KEY=dev-token   # optional; omit for open loopback lab
export REDIBIS_PII_GUARD_PACK=/path/to/pack.rdbpack  # optional
redibis-pii-guard
# → http://127.0.0.1:8090
```

## Endpoints

| Method | Path | Auth | Backend |
|--------|------|------|---------|
| GET | `/health` | no | composer |
| POST | `/scan` | yes* | ScanBackend |
| GET | `/ruleset` | yes* | ScanBackend / pack |
| POST | `/deidentify` | yes* | MaskBackend |
| GET | `/policies` | yes* | MaskBackend |
| POST | `/scan-and-mask` | yes* | composer |

\* When `REDIBIS_PII_GUARD_API_KEY` or `REDIBIS_PII_GUARD_REQUIRE_AUTH=1` is set.

## Auth

Vendor-issued shared secret (same model as the codegen lab API-key mode):

```
Authorization: Bearer <REDIBIS_PII_GUARD_API_KEY>
```

## Split seam

- `redibis_pii_guard.scan_backend` / `scan_routes` — detection only; no deid imports
- `redibis_pii_guard.mask_backend` / `mask_routes` — deid only; no scanner imports
- `PiiGuardService` composes both for `/scan-and-mask`

## Logging

Metadata only: char/row counts, entity counts, language, policy id, latency.
Never log request bodies or masking keys.
