# Tutorial — Full scan → enrich (local Qwen on SGLang) → OpenMetadata

End-to-end path: scan a CSV into a **full active ODCS contract** on **local disk**,
**LLM-enrich** it with a **self-hosted Qwen** model behind **SGLang**, then **push**
the contract into **OpenMetadata**.

For a **PII-only golden CSV** walkthrough with custom prompt/context files and
verified OM column definitions, see
[PII_CSV_ENRICH_OPENMETADATA_CLI.md](PII_CSV_ENRICH_OPENMETADATA_CLI.md).

| Step | Command | Result |
|------|---------|--------|
| 1 | `redibis scan … --automerge both` | Active contract (profile + quality + PII) |
| 2 | `redibis enrich …` (optional `--multistep`) | Descriptions / business fields / table narrative / whole-contract review |
| 3 | `redibis catalog push` or `push-scan` | Service → DB → schema → table + data contract in OM |

**Example configs in-repo:**

| File | Role |
|------|------|
| [`config/examples/scan-enrich-catalog-sglang.yaml`](../../config/examples/scan-enrich-catalog-sglang.yaml) | Complete `RedibisConfig` (local storage, automerge, OM catalog, RAI local) |
| [`config/examples/llm-providers-sglang-qwen.json`](../../config/examples/llm-providers-sglang-qwen.json) | Provider registry entry for SGLang + Qwen |
| [SCAN_ENRICH_CATALOG_SGLANG_CONFIG.md](SCAN_ENRICH_CATALOG_SGLANG_CONFIG.md) | **Field-by-field reference** — how to fill each key and allowed values |

---

## What you will have

```text
./tutorial_workspace/          # or repo root with --output-dir ./reports
  data/customers.csv           # your sample
  reports/                     # --output-dir (sessions + local store)
    _dev_storage/
      active-contracts/        # merged ODCS contracts
      pii-contracts/
      quality-contracts/
      pii-reports/
```

Local storage is the **default** (do **not** pass `--use-s3`). Keep the same
`--output-dir` on every command so scan, enrich, and catalog share one store.

---

## Prerequisites

```bash
cd /path/to/redibis
pip install -e ".[dev,enrich,catalog]" -c requirements/constraints.txt
```

| Piece | Notes |
|-------|--------|
| Sample CSV / Parquet | Path used as `FILE` below |
| SGLang + Qwen | OpenAI-compatible API on port **30000** (or change `api_base`) — managed separately |
| OpenMetadata ≥ 1.8 | e.g. `http://localhost:8585` + bot JWT in `OM_BOT_JWT` (1.12+ uses ODCS Data Contract import; &lt;1.12 falls back to the native mapper) |
| Same `--output-dir` | All three commands must point at the same local store |

> **Enterprise air-gap UAT** (venv/Conda rebuild, bare-metal OM, staged
> acceptance) lives in the vendor monorepo under
> `enterprise/deploy/airgap/baremetal/` (`CUSTOMER_AIRGAP_PLAYBOOK.md`) — not
> shipped in the public OSS extract.

Optional OM stack:```bash
./deployment_scripts/redibis.sh up --build --with-openmetadata
# or: cd open_metadata && docker compose up -d
curl -sf http://localhost:8585/api/v1/system/version | head -c 200
```

---

## 0. Environment

From the repo root (or any working directory that can resolve the example paths):

```bash
export REDIBIS_CONFIG=config/examples/scan-enrich-catalog-sglang.yaml
export REDIBIS_LLM_PROVIDERS=config/examples/llm-providers-sglang-qwen.json
export SGLANG_API_KEY=sk-local          # dummy key; OpenAI clients often need non-empty
export OM_BOT_JWT='eyJ...'              # from OM Settings → Bots

FILE=data/customers.csv                 # your sample path
TABLE=telecom.customers
OUT=./reports
mkdir -p "$(dirname "$FILE")" "$OUT"
```

To customize, copy the examples into a private workspace and point the env vars
at your copies:

```bash
mkdir -p tutorial_workspace/data tutorial_workspace/reports
cp config/examples/scan-enrich-catalog-sglang.yaml tutorial_workspace/redibis.yaml
cp config/examples/llm-providers-sglang-qwen.json tutorial_workspace/llm_providers.json
export REDIBIS_CONFIG=$PWD/tutorial_workspace/redibis.yaml
export REDIBIS_LLM_PROVIDERS=$PWD/tutorial_workspace/llm_providers.json
```

---

## 1. Complete config (`redibis.yaml`)

For every key, allowed values, and how to fill them, see
[SCAN_ENRICH_CATALOG_SGLANG_CONFIG.md](SCAN_ENRICH_CATALOG_SGLANG_CONFIG.md).

The in-repo example is the source of truth. Highlights:

- `storage.backend: local` — contracts under `--output-dir/_dev_storage/`
- `contract.automerge: both` — merge PII + quality into the active contract on scan
- `catalog.backend: openmetadata` — push target for step 3
- `rai.default_residency: local` — matches self-hosted SGLang
- top-level `llm.default.provider: sglang-qwen` — role routing for agents / capability layer

CLI enrich always selects the model via `--provider` (and optionally
`--providers-file` / `REDIBIS_LLM_PROVIDERS`), not via `RedibisConfig` fields alone.

See the full file:

[`config/examples/scan-enrich-catalog-sglang.yaml`](../../config/examples/scan-enrich-catalog-sglang.yaml)

---

## 2. LLM provider — local Qwen on SGLang

See:

[`config/examples/llm-providers-sglang-qwen.json`](../../config/examples/llm-providers-sglang-qwen.json)

**Model id must match what SGLang serves.** Check:

```bash
curl -s http://localhost:30000/v1/models | jq .
```

If your served name differs, change both `litellm_model` (keep the `openai/` prefix)
and `--model` / the YAML `llm.default.model`.

If your SGLang build rejects `response_format=json_object`, set
`"supports_json": false` — enrichment still asks for JSON in the prompt and parses the reply.

### Start SGLang (example)

```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-14B-Instruct \
  --served-model-name Qwen/Qwen2.5-14B-Instruct \
  --host 0.0.0.0 --port 30000
```

### Register via CLI instead of editing JSON (optional)

```bash
redibis llm add sglang-qwen --preset sglang-qwen \
  --model Qwen/Qwen2.5-14B-Instruct \
  --api-base http://localhost:30000/v1 \
  --param timeout=120 --param max_tokens=4096
```

### Probe before enriching

```bash
redibis llm test sglang-qwen --api-key sk-local -v --staged
```

Expect reachability → completion → (optional) json_mode stages to pass.

---

## 3. Scan — full contract with automerge (local storage)

```bash
redibis scan "$FILE" "$TABLE" \
  --config "$REDIBIS_CONFIG" \
  --mode all \
  --automerge both \
  --output-dir "$OUT"
```

- `--mode all` — profile + quality + PII
- `--automerge both` — merge both subcontracts into **active** (also set in YAML)
- Storage: `$OUT/_dev_storage/` (no `--use-s3`)

Verify:

```bash
redibis show "$TABLE" --output-dir "$OUT"
redibis list --output-dir "$OUT"
```

You should see an active contract for `telecom.customers` with schema, quality rules, and PII tags.

---

## 4. Enrich — local Qwen via SGLang

```bash
redibis enrich "$TABLE" \
  --config "$REDIBIS_CONFIG" \
  --providers-file "$REDIBIS_LLM_PROVIDERS" \
  --provider sglang-qwen \
  --model Qwen/Qwen2.5-14B-Instruct \
  --endpoint http://localhost:30000/v1 \
  --api-key sk-local \
  --output-dir "$OUT"
```

Multistep (YAML stages + ODCS validate/retry; table description and whole-contract
review are included in the default example):

```bash
redibis enrich "$TABLE" \
  --config "$REDIBIS_CONFIG" \
  --providers-file "$REDIBIS_LLM_PROVIDERS" \
  --provider sglang-qwen \
  --model Qwen/Qwen2.5-14B-Instruct \
  --endpoint http://localhost:30000/v1 \
  --api-key sk-local \
  --multistep \
  --steps-file /path/to/config/examples/enrich-steps-default.yaml \
  --output-dir "$OUT"
```

On a **valid** enrichment, redibis **auto-writes** into the active contract
(`Auto-written to active → vN`). No extra merge step is required.

Optional dry-run (prompts only, no LLM write):

```bash
redibis enrich "$TABLE" \
  --config "$REDIBIS_CONFIG" \
  --providers-file "$REDIBIS_LLM_PROVIDERS" \
  --provider sglang-qwen \
  --dry-run \
  --output-dir "$OUT"
```

Export the reusable prompt profile (one-call `normal/` + per-stage `multistep/`), then
attach a company glossary that every stage sees:

```bash
redibis enrich export-context "$OUT/context-profile" --output-dir "$OUT"
redibis enrich add-context ./docs/domain_glossary.md \
  --scope global --mode shared --output-dir "$OUT"
redibis enrich add-context ./docs/pii-policy.md \
  --scope global --mode multistep --stage classification_pii --output-dir "$OUT"

# Re-run enrich using the exported profile as --pack
redibis enrich "$TABLE" \
  --config "$REDIBIS_CONFIG" \
  --providers-file "$REDIBIS_LLM_PROVIDERS" \
  --provider sglang-qwen \
  --pack "$OUT/context-profile" \
  --multistep \
  --output-dir "$OUT"
```

Offline smoke test (no SGLang):

```bash
redibis enrich "$TABLE" --provider demo --output-dir "$OUT"
```

Re-check the contract:

```bash
redibis show "$TABLE" --output-dir "$OUT" | head -n 80
```

Fetch LLM evidence for a run id printed by enrich:

```bash
redibis get llm-call-logs <run_id> --zip enrich-evidence.zip --output-dir "$OUT"
# shareable only — no evidence_bundle.json / *.raw.json
```

---

## 5. Push to OpenMetadata

```bash
# OM_BOT_JWT and REDIBIS_CONFIG already exported

redibis catalog push "$TABLE" \
  --backend openmetadata \
  --config "$REDIBIS_CONFIG" \
  --output-dir "$OUT"
```

For a **folder of scan sessions** (many tables), prefer resumable batch:

```bash
redibis catalog push-scan "$OUT" --config "$REDIBIS_CONFIG" --output-dir "$OUT"
# after partial failures:
redibis catalog push-scan "$OUT" --resume <run_id> --config "$REDIBIS_CONFIG" --output-dir "$OUT"
```

OM **1.12+** attaches Data Contracts via ODCS import; **&lt; 1.12** (e.g. 1.11.13) uses the
native mapper. Details: [CATALOG_OPENMETADATA_TUTORIAL.md](CATALOG_OPENMETADATA_TUTORIAL.md).

Dry-run first:

```bash
redibis catalog push "$TABLE" \
  --backend openmetadata \
  --config "$REDIBIS_CONFIG" \
  --output-dir "$OUT" \
  --dry-run --json
```

Status / UI URL:

```bash
redibis catalog status "$TABLE" --config "$REDIBIS_CONFIG" --output-dir "$OUT"
redibis catalog open "$TABLE" --config "$REDIBIS_CONFIG" --output-dir "$OUT"
```

Typical FQN:

```text
redibis.telecom.default.customers
```

OpenMetadata UI: `http://localhost:8585` → that table → Data Contract + tags/glossary as configured under `catalog.push`.

---

## 6. One-liner chain

```bash
redibis scan "$FILE" "$TABLE" \
  --config "$REDIBIS_CONFIG" --mode all --automerge both --output-dir "$OUT" && \
redibis enrich "$TABLE" \
  --config "$REDIBIS_CONFIG" \
  --providers-file "$REDIBIS_LLM_PROVIDERS" \
  --provider sglang-qwen \
  --model Qwen/Qwen2.5-14B-Instruct \
  --endpoint http://localhost:30000/v1 \
  --api-key sk-local \
  --output-dir "$OUT" && \
redibis catalog push "$TABLE" \
  --backend openmetadata \
  --config "$REDIBIS_CONFIG" \
  --output-dir "$OUT"
```

---

## Cleanup (optional)

Remove OpenMetadata entities created by this tutorial. Dry-run unless `--yes`.
Local contracts under `$OUT` are separate — delete those with `rm` if needed.

```bash
# One table
redibis catalog delete "$TABLE" \
  --config "$REDIBIS_CONFIG" --output-dir "$OUT"
redibis catalog delete "$TABLE" \
  --config "$REDIBIS_CONFIG" --output-dir "$OUT" --yes

# Or wipe the whole Redibis OM service (+ glossary / classifications)
redibis catalog wipe --config "$REDIBIS_CONFIG" --yes \
  --with-glossary --with-classifications
```

Details: [../cli/catalog.md](../cli/catalog.md) · [CATALOG_OPENMETADATA_TUTORIAL.md](CATALOG_OPENMETADATA_TUTORIAL.md#delete-tables-or-wipe-the-redibis-om-service).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Enrich fails with wrong model | `curl localhost:30000/v1/models` — align `--model` / `litellm_model` with served name |
| `api_base` errors | Must include `/v1` → `http://localhost:30000/v1` |
| Empty / missing API key | `export SGLANG_API_KEY=sk-local` or `--api-key sk-local` |
| JSON mode rejected by SGLang | Set `"supports_json": false` in the providers JSON |
| Timeout / truncated enrich | Raise SGLang `--context-length`; raise `params.timeout` / `max_tokens` |
| `No active contract` on enrich/push | Same `--output-dir` as scan; confirm `redibis list --output-dir …` |
| Catalog 401 | Refresh bot JWT → `OM_BOT_JWT` |
| OM not reachable | `curl http://localhost:8585/api/v1/system/version` |
| Data Contract `Unrecognized field "status"` | OM &lt; 1.12 — redibis uses native mapper automatically; prefer OM 1.12+ for ODCS import |
| Batch many tables after scan | `redibis catalog push-scan "$OUT"` — see [CATALOG_OPENMETADATA_TUTORIAL.md](CATALOG_OPENMETADATA_TUTORIAL.md)#push-from-a-scan-folder-push-scan |
| Multistep / table description / review | `redibis enrich … --multistep --steps-file config/examples/enrich-steps-default.yaml` |
| Export prompt profile | `redibis enrich export-context ./context-profile` |
| Add custom context | `redibis enrich add-context FILE --scope global --mode shared` |

---

## Related docs

- [SCAN_ENRICH_CATALOG_SGLANG_CONFIG.md](SCAN_ENRICH_CATALOG_SGLANG_CONFIG.md) — config field reference
- [CLI_SCAN_TUTORIAL.md](CLI_SCAN_TUTORIAL.md) — scan / automerge detail
- [CATALOG_OPENMETADATA_TUTORIAL.md](CATALOG_OPENMETADATA_TUTORIAL.md) — catalog push / push-scan / delete / wipe
- [../cli/catalog.md](../cli/catalog.md) — catalog CLI cheat sheet · [../cli/enrich.md](../cli/enrich.md)
- [LLM_PROVIDERS.md](../LLM_PROVIDERS.md) — SGLang / custom profiles
- [LLM_ENRICHMENT.md](../LLM_ENRICHMENT.md) — enrich architecture & artifacts
