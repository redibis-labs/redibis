# Config reference — `scan-enrich-catalog-sglang.yaml`

Field-by-field guide for
[`config/examples/scan-enrich-catalog-sglang.yaml`](../../config/examples/scan-enrich-catalog-sglang.yaml).

Use this when copying the example and filling in **your** table name, paths,
OpenMetadata host, and local Qwen / SGLang endpoint.

| Related | Path |
|---------|------|
| Example YAML | `config/examples/scan-enrich-catalog-sglang.yaml` |
| LLM providers JSON | `config/examples/llm-providers-sglang-qwen.json` |
| End-to-end tutorial | [SCAN_ENRICH_CATALOG_SGLANG.md](SCAN_ENRICH_CATALOG_SGLANG.md) |
| Schema source | `redibis/config.py` (`RedibisConfig`) |

Load / validate:

```bash
export REDIBIS_CONFIG=config/examples/scan-enrich-catalog-sglang.yaml
python -c "from redibis.config import RedibisConfig; print(RedibisConfig.from_yaml('$REDIBIS_CONFIG').table)"
# or dump defaults to start from scratch:
redibis config dump-default -o my-redibis.yaml
```

---

## How to fill it in (quick checklist)

1. **`table`** — set to your `schema.table` (must match CLI `redibis scan … TABLE`).
2. **`scan_types` / `contract.automerge`** — keep `profile+quality+pii` and `both` for a full active contract.
3. **`storage.backend`** — leave `local` for disk under `--output-dir/_dev_storage/`.
4. **`catalog.openmetadata.*` + `OM_BOT_JWT`** — point at your OM UI and bot token.
5. **`llm_providers.json` (separate file)** — set `api_base`, model id, and `SGLANG_API_KEY`.
6. Leave **`memory` / `classification` / `agents` / `behavior`** off unless you need those features.

Env vars this example expects:

| Env | Purpose |
|-----|---------|
| `REDIBIS_CONFIG` | Path to this YAML |
| `REDIBIS_LLM_PROVIDERS` | Path to `llm-providers-sglang-qwen.json` |
| `SGLANG_API_KEY` | Dummy or real key for SGLang (often `sk-local`) |
| `OM_BOT_JWT` | OpenMetadata bot JWT (name matches `catalog.openmetadata.jwt_env`) |

---

## Top-level identity

### `table`

| | |
|-|-|
| **Type** | string |
| **Example** | `telecom.customers` |
| **What to put** | Logical `database.table` (or `schema.table`) identity for the contract. Must match the table argument on `redibis scan` / `enrich` / `catalog push`. |
| **Possible values** | Any `name.name` string; conventionally lowercase with a single dot. Becomes `physicalName` / catalog FQN pieces. |

### `scan_types`

| | |
|-|-|
| **Type** | list of strings |
| **Example** | `[profile, quality, pii]` |
| **What to put** | Which scan phases run when the config drives a scan. |
| **Possible values** | `profile`, `quality`, `pii` (any non-empty subset). Validated against `_VALID_SCAN_TYPES`. |

Notes:

- `quality` implies profiling work as well in the engine.
- CLI `--mode all|profile|pii|quality|…` can override per invocation.

---

## `source` — catalog / pushdown connection

Used only when profiling **metadata** or **pushdown** tiers are enabled (both off in this example).

| Field | Type | Default / example | Possible values / how to fill |
|-------|------|-------------------|-------------------------------|
| `engine` | string | `none` | `none`, `hive`, `postgres`, `oracle`, `jdbc` |
| `spark_session_injected` | bool | `true` | Keep `true` when Spark session is injected by the host |
| `jdbc.host` | string | `""` | JDBC host when `engine` is `jdbc` / DB engines |
| `jdbc.port` | int | `0` | Port (e.g. `5432`) |
| `jdbc.database` | string | `""` | Database / service name |
| `jdbc.credential_ref` | string | `""` | Opaque credential reference (never put passwords in YAML) |

For a file-only CSV scan, leave `engine: none`.

---

## `profiling`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `engine` | string | `great_expectations` | `great_expectations`, `duckdb`, `open_metadata` (`PROFILER_REGISTRY`) |
| `triage_threshold` | float | `0.0` | `0.0`–`1.0` — GE triage / nullish floor; `0.0` = do not skip columns |
| `arabic_threshold` | float | `0.05` | Fraction of Arabic script that triggers Arabic-aware handling |
| `metadata.enabled` | bool | `false` | `true` to pull Tier-A catalog metadata (needs `source.engine`) |
| `pushdown.enabled` | bool | `false` | `true` for Tier-B SQL aggregates on catalog gaps |
| `pushdown.approx_distinct` | bool | `true` | Use approx distinct when pushdown is on |
| `openmetadata.include_frequent_values` | bool | `true` | Only when `engine: open_metadata` |
| `openmetadata.max_frequent_values` | int | `10` | Cap on frequent-value samples |

**Fill tip:** keep `great_expectations` for parity with quality docs; use `duckdb` for faster GE-free profiling.

---

## `quality`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `generate_ge_docs` | bool | `true` | `true` writes GE Data Docs into the run; `false` speeds up scans (`--no-ge-docs`) |
| `rule_set` | object | *(omitted → empty)* | Optional curated rules. Shape: `{ name: string, rules: [ { rule, column?, kwargs?, meta? } ] }` |

Example curated rule:

```yaml
quality:
  generate_ge_docs: true
  rule_set:
    name: merchant-baseline
    rules:
      - rule: expect_column_values_to_not_be_null
        column: merchant_email
        kwargs: { mostly: 0.99 }
```

If `rule_set` is empty, the profiler / gatekeeper suggest and run expectations from the sample.

---

## `pii`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `engines` | string | `both` | `regex`, `gliner`, `ner`, `llm`, `both` (`ner` ≈ GLiNER path; `both` = regex + NER) |
| `equation_mode` | string | `balanced` | `strict`, `balanced`, `lenient`, `independent` (default in code is `independent`) |
| `sample_size` | int | `100` | Max cells sampled per column for detection |
| `use_phonenumbers` | bool | `true` | Enable libphonenumber gate for phone-like columns |
| `default_region` | string | `EG` | ISO region for phone parsing (e.g. `EG`, `US`, `SA`) |
| `msisdn_valid_rate_min` | float | `0.80` | Min valid MSISDN rate before phone vote counts |
| `phone_gate_conf` | float | `0.10` | Confidence floor for phone gate |
| `geo_require_pair` | bool | `true` | Require lat/lon pairing heuristics for geo PII |
| `geo_egypt_geofence` | bool | `false` | Egypt-specific geofence post-process |
| `msisdn_prefixes` | list[string] | Egyptian mobile prefixes | Local number prefixes to treat as MSISDN candidates |
| `models_dir` | string | `/models` | Root for packaged NER weights (optional) |
| `selected_columns` | list[string] \| null | *(omit)* | Limit PII scan to named columns |
| `regex_overrides` | object \| null | *(omit)* | Advanced regex catalog overrides |
| `gliner_always_run` | bool | `false` | Force NER even when regex is already high-confidence |

### `pii.ner`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `type` | string | `gliner` | Backend id (commonly `gliner`) |
| `model_path` | string | `""` | Local weights directory, or set `REDIBIS_NER_MODEL`; empty = packaged/default resolution |
| `device` | string | `cpu` | `cpu`, `cuda`, or device string your stack accepts |
| `threshold` | float | `0.3` | NER score floor |
| `batch_size` | int | `8` | Inference batch size |
| `labels` | list[string] | `[]` | Optional entity labels override |
| `label_groups` | list[list[string]] | `[]` | Multi-pass label groups |
| `models` | list[object] | `[]` | Multi-model NER registry entries |
| `max_models` / `max_passes` | int | `4` / `8` | Caps for multi-model / multi-pass |
| `always_run` | bool | `false` | Same intent as `gliner_always_run` |

### `pii.llm` (optional PII **refiner** — not full-contract enrich)

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `enabled` | bool | `false` | Keep `false` unless you want LLM ambiguity resolution during PII |
| `provider` | string | `sglang` | Name from `llm_providers.json` (e.g. `sglang`, `sglang-qwen`, `ollama`) |
| `model_name` | string | `Qwen/Qwen2.5-14B-Instruct` | Must match the served model id |
| `endpoint_url` | string | `http://localhost:30000/v1` | OpenAI-compatible base including `/v1` |
| `api_key` | string | `""` | Prefer env vars; avoid committing secrets |
| `temperature` | float | `0.0` | Sampling temperature for the refiner |

### `pii.thresholds` (optional; not in the example file)

Per-engine confidence floors (defaults):

| Key | Default | Meaning |
|-----|---------|---------|
| `presidio_min` | `0.8` | Presidio / regex vote floor |
| `gliner_min` | `0.7` | NER vote floor |
| `llm_min` | `0.82` | LLM refiner vote floor |
| `phone_min` | `0.8` | Phone engine floor |
| `learned_min` | `0.9` | Learned classifier floor |
| `ge_triage_min` | `0.0` | GE triage floor |
| `very_high_confidence_floor` | `0.9` | “Very high” band |

```yaml
pii:
  thresholds:
    gliner_min: 0.65
    phone_min: 0.85
```

---

## `storage`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `backend` | string | `local` | `local` (disk under `--output-dir/_dev_storage`) or use CLI `--use-s3` for MinIO/S3 |
| `endpoint_url` | string \| null | *(omit)* | S3/MinIO endpoint when not local (e.g. `http://localhost:9000`) |
| `runs_bucket` | string | `pii-reports` | Run artifacts bucket / folder name |
| `contracts_bucket` | string | `active-contracts` | Active ODCS contracts |
| `pii_runs_bucket` | string | `pii-contracts` | PII subcontracts |
| `quality_runs_bucket` | string | `quality-contracts` | Quality subcontracts |

**Fill tip:** for this tutorial keep `local` and pass the same `--output-dir` on every command. Do not put credentials in YAML — use `S3_ACCESS_KEY` / `S3_SECRET_KEY` (or AWS equivalents) when using S3.

---

## `contract`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `auto_create` | bool | `true` | Create an active contract shell if missing |
| `auto_write` | bool | `false` | Legacy flag; prefer `automerge` |
| `automerge` | string | `both` | `none`, `pii`, `quality`, `both` |
| `validate` | bool | `true` | ODCS validation before merge (`--no-validate` to skip) |

**Fill tip:** `both` delivers a full active contract after scan (PII + quality). Use `none` for review-first (`redibis runs merge …` later).

---

## `report`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `formats` | list[string] | `[profile, quality, pii]` | Artifact kinds to emit; typically subset of scan types |
| `output_dir` | path | `./reports` | Default report/session root (CLI `--output-dir` still wins for store layout) |

---

## `enrich`

Controls **full-contract** enrichment context (CLI `redibis enrich`), not the PII refiner.

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `include_pii_evidence` | bool | `false` | `true` adds PII evidence into the enrich prompt (more signal, more sensitive) |
| `max_attempts` | int | `3` | LLM / ODCS repair retries for multistep (and stage defaults) |
| `similar_context.enabled` | bool | `false` | `true` pulls similar-column memory hints (needs `memory.enabled`) |
| `similar_context.k` | int | `5` | How many similar columns to include |
| `context.pack` | path | `./context-profile` | Enrichment pack folder / `.zip` / `.rdbpack` / exported profile DIR. Empty = prompt store then builtins |
| `context.custom_dir` | path | `""` | Global overlay root. Empty = `$REDIBIS_CONFIGS_DIR/enrich-context/custom` |
| `multistep.enabled` | bool | `false` | `true` runs YAML stages without passing `--multistep` |
| `multistep.steps_file` | path | `config/examples/enrich-steps-default.yaml` | Workflow YAML (prebuilt stages only) |
| `multistep.rai_enabled` | bool / null | `false` | `null` = use `rai.*`; `false` for local LLMs |
| `multistep.review_enabled` | bool | `true` | Extra whole-contract LLM pass after the other stages |
| `multistep.review_max_changes` | int | `25` | Cap on corrections the review stage may apply |

Provider / model for enrich come from **`llm_providers.json`** + CLI flags (`--provider`, `--model`, `--endpoint`), not from these fields alone.

```yaml
enrich:
  include_pii_evidence: false
  max_attempts: 3
  context:
    pack: ./context-profile
    custom_dir: ""
  multistep:
    enabled: false
    steps_file: config/examples/enrich-steps-default.yaml
    rai_enabled: false
    review_enabled: true
    review_max_changes: 25
  similar_context:
    enabled: true
    k: 5
```

Export / customize prompts (see [cli/enrich.md](../cli/enrich.md)#context-profiles):

```bash
redibis enrich export-context ./context-profile --output-dir ./reports
redibis enrich add-context glossary.md --scope global --mode shared --output-dir ./reports
```

---

## `llm` (top-level role routing)

Optional block for **agent / capability routing**. `RedibisConfig.from_yaml` does not map it onto a dataclass field; the webapp / routing layer reads raw YAML / global settings. CLI enrich still uses `--provider`.

```yaml
llm:
  default:
    provider: sglang-qwen          # MUST match a key in llm_providers.json
    model: Qwen/Qwen2.5-14B-Instruct
  roles:
    contract.enrichment:
      inherit: default             # or set provider/model/enabled explicitly
```

### `llm.default`

| Field | What to put |
|-------|-------------|
| `provider` | Registry name: `sglang-qwen`, `sglang`, `ollama`, `vllm`, `demo`, … |
| `model` | Exact served model id from `GET {api_base}/models` |

### `llm.roles.<role>`

Known role ids (`MODEL_ROLES`):

- `agent.planner`, `agent.planner_repair`, `agent.router`, `agent.copilot`
- `contract.enrichment`, `contract.enrichment_repair`
- `pii.refiner`
- `codegen.generator`, `codegen.judge`
- `behavior.draft`, `classification.judge`
- `pack.evaluator`, `provider.probe`

Per-role fields you can set:

| Field | Possible values |
|-------|-----------------|
| `inherit` | Another role name or `default` |
| `enabled` | `true` / `false` |
| `provider` / `model` | Override inherited binding |
| `residency_policy` | `local_only`, `private_or_local`, `any`, `masked_external` |
| `fallback` | `heuristic`, `template`, `skip`, `fail` |
| `params` | Extra LiteLLM kwargs (temperature, timeout, …) |

---

## `rai` — responsible AI middleware

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `enabled` | bool | `true` | Master switch for RAI middleware |
| `mode` | string | `report` | `report`, `warn`, `block` |
| `enforce` | bool | `false` | `true` to hard-fail on violations (enterprise) |
| `hard_block_external_pii` | bool | `false` | Block sending raw PII to external models |
| `block_external_raw_pii` | bool | `true` | Soft/hard policy depending on enforce |
| `default_residency` | string | `local` | Expected residency for this deploy (`local` for SGLang) |
| `allowed_models` / `denied_models` | list[string] | `[]` | Optional allow/deny model id lists |
| `log_prompts` | bool | `true` | Log prompt metadata (careful in prod) |

For local Qwen, keep `default_residency: local` and `enforce: false` unless you need gatekeeping.

---

## `catalog` — OpenMetadata (or Atlas / DataHub)

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `backend` | string | `openmetadata` | `openmetadata`, `atlas`, `datahub` |
| `push.tags` | bool | `true` | Push PII / classification tags |
| `push.glossary` | bool | `true` | Upsert glossary terms |
| `push.contract` | bool | `true` | Attach ODCS as table data contract (OM ≥1.12 ODCS import; &lt;1.12 native mapper) |
| `push.masked_samples` | bool | `false` | Upload masked samples (usually off) |
| `push.lifecycle_diff` | bool | `true` | Lifecycle / diff metadata on push |

Batch many tables after a scan folder: `redibis catalog push-scan <scan-dir>` (see [catalog.md](../cli/catalog.md)).

### `catalog.openmetadata`

| Field | Type | Example | How to fill |
|-------|------|---------|-------------|
| `host` | string | `http://localhost:8585` | OM UI / API base (no trailing slash required) |
| `jwt_env` | string | `OM_BOT_JWT` | **Name of the env var** holding the bot JWT — not the token itself |
| `service_name` | string | `redibis` | OM service node under which databases appear |
| `default_schema` | string | `default` | Schema segment when contract has no schema |

FQN pattern: `{service_name}.{database}.{default_schema}.{table_name}`  
Example: `telecom.customers` → `redibis.telecom.default.customers`

### Other backends (if you switch `backend`)

**Atlas** (`catalog.atlas`): `host`, `username_env`, `password_env`, `cluster`, `entity_type`  
**DataHub** (`catalog.datahub`): `host`, `token_env`, `platform`, `env`, `actor`

---

## `memory` — column learning loop (off here)

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `enabled` | bool | `false` | Turn on for enrich similar-column memory |
| `store` | string | `pgvector` | `pgvector`, `memory` (in-process) |
| `dsn_ref` | string | `""` | Env/ref name for Postgres DSN (never inline secrets) |
| `embedding_provider` | string | `sentence_transformers` | Embedding backend id |
| `embedding_model` | string | `all-MiniLM-L6-v2` | Model name for embeddings |
| `top_k` | int | `5` | Retrieval size |
| `min_similarity` | float | `0.0` | Similarity floor |
| `domain` | string | `""` | Optional domain tag for ranking |
| `async_writes` | bool | `true` | Async memory writer |

See also `config/examples/memory-pgvector.yaml`.

---

## `classification`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `enabled` | bool | `false` | Multi-domain classification engine |
| `policy_pack` | string | `telecom` | Built-in pack name when enabled |
| `policy_path` | string \| null | *(omit)* | Custom policy YAML path |
| `default_jurisdiction` | string | `""` | Jurisdiction code if used by pack |
| `edge_rules_enabled` | bool | `true` | Edge compatibility rules |
| `per_run_overlay_allowed` | bool | `true` | Allow per-run overlays |

---

## `agents`

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Off for this CLI-only tutorial |
| `runs_dir` | string | `./agent_runs` | Agent run folders when enabled |
| `planner_provider` / `planner_model` | string | `""` | IntentPlanner defaults |
| `copilotkit_enabled` | bool | (default on in code) | AG-UI chat |
| `auto_approve_writes` | bool | (default on) | Local-dev friendly; lock down in prod |
| `single_table_executor` / `batch_executor` | string | `langgraph` | `langgraph` or legacy modes |

See `config/examples/agents-local-full.yaml` for a full agentic stack.

---

## `observability`

| Field | Type | Example | Possible values / how to fill |
|-------|------|---------|-------------------------------|
| `log_level` | string | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `log_format` | string | `""` | `rich`, `json`, `plain`, or empty (auto) |
| `log_dir` | string | `""` | Optional directory for log files |
| `log_samples` | bool | `false` | Log sample cell values (sensitive) |
| `persist_run_log` | bool | `true` | Keep per-run logs |
| `decision_log` | bool | `true` | Persist decision traces |
| `llm_debug` / `llm_log_prompts` | bool | `false` | Verbose LLM diagnostics |
| `module_levels` | map | `{}` | e.g. `{ "redibis.pii": "DEBUG" }` |
| `otel.enabled` | bool | `false` | Needs `redibis[otel]` |
| `otel.exporter` | string | `file` | Exporter id |
| `otel.otlp_endpoint` | string | `""` | OTLP URL when used |
| `otel.service_name` | string | `redibis` | Span service name |

---

## `behavior`

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Behavior Policy Runtime; default off keeps baseline engines unchanged |

When enabled, additional fields live in `redibis.behavior.config.BehaviorConfig` (mode, fail_mode, store paths). See `docs/BEHAVIOR_POLICY_ARCHITECTURE.md`.

---

## Companion file: `llm-providers-sglang-qwen.json`

Not part of `RedibisConfig`, but required for enrich with local Qwen.

| Field | Example | How to fill |
|-------|---------|-------------|
| `providers.sglang-qwen.api_base` | `http://localhost:30000/v1` | Must include `/v1` |
| `litellm_model` | `openai/Qwen/Qwen2.5-14B-Instruct` | `openai/` + exact served name |
| `api_key_env` | `SGLANG_API_KEY` | Env var name; set `export SGLANG_API_KEY=sk-local` |
| `supports_json` | `true` | Set `false` if SGLang rejects `response_format=json_object` |
| `params.timeout` / `max_tokens` | `120` / `4096` | Raise for large contracts |
| `residency` | `local` | Marks provider as local for RAI |

Verify model id:

```bash
curl -s http://localhost:30000/v1/models | jq .
```

---

## Minimal edit map (copy-paste)

Change only these when adapting the example to a new table / host:

```yaml
table: mydb.my_table                    # ← your identity

catalog:
  openmetadata:
    host: http://om.example.com:8585    # ← your OM
    jwt_env: OM_BOT_JWT                 # ← env that holds the JWT
    service_name: redibis
    default_schema: default

# In llm-providers-sglang-qwen.json:
#   api_base → your SGLang URL
#   litellm_model / known_models → your Qwen served name
```

Everything else can stay as in the example for a local full scan → enrich → catalog push.

---

## Related

- [SCAN_ENRICH_CATALOG_SGLANG.md](SCAN_ENRICH_CATALOG_SGLANG.md) — commands
- [CATALOG_OPENMETADATA_TUTORIAL.md](CATALOG_OPENMETADATA_TUTORIAL.md) — OM push
- [LLM_PROVIDERS.md](../LLM_PROVIDERS.md) — SGLang / custom profiles
- `redibis config dump-default` — full default YAML skeleton
