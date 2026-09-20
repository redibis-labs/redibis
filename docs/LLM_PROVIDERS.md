# LLM providers — configuration and connectivity testing

Redibis routes every enrichment provider through **LiteLLM** (`redibis.enrich.providers`).
Providers are declared in JSON — no code changes required to add a vendor.

All enrichment entry points (web, CLI, agent) call the same `EnrichmentService.build_context()` +
`enrich()` path — see [`docs/LLM_ENRICHMENT.md`](LLM_ENRICHMENT.md).

## Quick reference — vLLM, Ollama, Gemini

Install the enrichment extra once:

```bash
pip install -e ".[enrich]" -c requirements/constraints.txt
```

List registered providers and defaults:

```bash
redibis llm list
# same listing:
redibis enrich --list-providers
```

**CLI cheat sheet:** [`docs/cli/llm.md`](cli/llm.md) (list / test / add / remove).

Enrichment accepts either the **active store contract** or a **YAML/JSON file** (`--contract` / `-f`).
See [Enrichment CLI examples](#enrichment-cli-examples) below.

| Provider | Type | Default model (packaged) | API key / endpoint |
|----------|------|--------------------------|--------------------|
| `vllm` | Local (OpenAI-compatible) | `Qwen/Qwen2.5-7B-Instruct` | `api_base` → `http://localhost:8001/v1`; optional `VLLM_API_KEY` |
| `ollama` | Local | `llama3.3` | `api_base` → `http://localhost:11434` (no key) |
| `sglang` | Local (OpenAI-compatible; alias `slang`) | `default` | `api_base` → `http://localhost:30000/v1` |
| `gemini` | Cloud | `gemini-3.5-flash` | `GEMINI_API_KEY` env var |
| `openai` | Cloud | `gpt-4.1` | `OPENAI_API_KEY` |
| `claude` | Cloud | `claude-sonnet-5` | `ANTHROPIC_API_KEY` |

Connectivity without enriching a contract: ``redibis llm test <provider>`` (see [CLI probe](#cli--redibis-llm-listtest)).

---

## vLLM (local / self-hosted)

vLLM exposes an OpenAI-compatible HTTP API. Redibis uses LiteLLM’s `hosted_vllm/` prefix.

**1. Start vLLM** (example — adjust model and port to your cluster):

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-7B-Instruct \
  --host 0.0.0.0 --port 8001
```

**2. Optional:** if your server expects a key, export it (must match what vLLM was started with):

```bash
export VLLM_API_KEY=your-local-key   # only if your vLLM instance requires it
```

**3. Point redibis at your server** — either edit packaged defaults via `./llm_providers.json`:

```json
{
  "providers": {
    "vllm": {
      "litellm_model": "hosted_vllm/Qwen/Qwen2.5-7B-Instruct",
      "model_prefix": "hosted_vllm",
      "api_base": "http://localhost:8001/v1",
      "api_key_env": "VLLM_API_KEY",
      "supports_json": true,
      "params": { "temperature": 0.2 }
    }
  }
}
```

…or override the endpoint for one run:

```bash
redibis enrich telecom.customers --provider vllm \
  --endpoint http://localhost:8001/v1 \
  --model Qwen/Qwen2.5-7B-Instruct
```

**4. Enrich from the active contract:**

```bash
redibis enrich telecom.customers --provider vllm
redibis enrich telecom.customers --provider vllm --multistep
redibis enrich export-context ./context-profile
```

Full flag list, review stage, and context overlays: [`docs/cli/enrich.md`](cli/enrich.md).

**5. Enrich from a YAML file** (no prior active contract required):

```bash
redibis enrich --contract runs/2026-05-09/contract.deterministic.yaml --provider vllm
# table is resolved from physicalName in the file; or pass explicitly:
redibis enrich telecom.customers -f my_contract.yaml --provider vllm
```

**Python:**

```python
from redibis.enrich.providers import get_provider
from redibis.enrich.service import enrichment_service_for_store

provider = get_provider("vllm", model="Qwen/Qwen2.5-7B-Instruct", endpoint_url="http://localhost:8001/v1")
svc = enrichment_service_for_store(contract_store)
result = svc.enrich("telecom.customers", provider, enriched_by="script")
print(result.valid, result.version_after)
```

Local providers use `sample_policy=raw` — per-column samples may appear in the LLM context; output contracts are still scrubbed before write.

---

## Ollama (local)

**1. Install and pull a model:**

```bash
ollama pull llama3.3
ollama serve   # default http://localhost:11434
```

**2. CLI — default packaged provider:**

```bash
redibis enrich telecom.customers --provider ollama
```

**3. Override model** (bare name is auto-prefixed with `ollama/`):

```bash
redibis enrich telecom.customers --provider ollama --model qwen2.5
```

**4. Custom Ollama host:**

```bash
redibis enrich telecom.customers --provider ollama \
  --endpoint http://localhost:11434 \
  --model llama3.3
```

**Connectivity probe:**

```bash
redibis llm test ollama
redibis llm test ollama --model qwen2.5 --prompt "Say ping"
```

**5. From contract file:**

```bash
redibis enrich -f contract.deterministic.yaml --provider ollama --model llama3.3
```

**Python:**

```python
provider = get_provider("ollama", model="llama3.3", endpoint_url="http://localhost:11434")
result = enrichment_service_for_store(store).enrich("telecom.customers", provider)
```

**Note:** JSON mode depends on the model. If enrichment fails with a parse error, try a model known to follow JSON instructions (e.g. `llama3.3`, `qwen2.5`) or check `redibis enrich --list-providers` for `supports_json`.

---

## SGLang (local OpenAI-compatible)

SGLang serves models behind an OpenAI Chat Completions API (default port **30000**).
Registered as provider ``sglang`` (CLI alias ``slang``). For a freely editable profile
(recommended when debugging Qwen), create a **custom** provider — see
[Custom OpenAI-compatible profiles](#custom-openai-compatible-profiles) below.

**1. Start SGLang** (example):

```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-14B-Instruct \
  --served-model-name Qwen/Qwen2.5-14B-Instruct \
  --host 0.0.0.0 --port 30000
```

**2. Probe + enrich (packaged `sglang` entry):**

```bash
redibis llm test sglang --endpoint http://localhost:30000/v1 \
  --model Qwen/Qwen2.5-14B-Instruct --staged

redibis enrich telecom.customers --provider sglang \
  --endpoint http://localhost:30000/v1 \
  --model Qwen/Qwen2.5-14B-Instruct
```

If the server requires a key: `--api-key sk-local` (OpenAI clients often need any non-empty key).

### SGLang gotchas

1. **Model id must equal the served name** — check `GET http://localhost:30000/v1/models`. SGLang serves under `--model-path` unless `--served-model-name` is set. Wrong id is the #1 enrichment failure.
2. **Non-empty API key** — even locally, use a dummy (`sk-local`) via session key or `SGLANG_API_KEY`.
3. **`response_format=json_object`** — some SGLang builds reject it. Set `supports_json: false` on the profile (Enrich UI toggle); enrichment still asks for JSON in the prompt and parses the reply.
4. **`api_base` must include `/v1`** — e.g. `http://localhost:30000/v1`.
5. **Context / timeout** — long enrichment prompts need enough `--context-length` on the server and higher `params.timeout` / `max_tokens` in the profile.

---

## Custom OpenAI-compatible profiles

Create a named profile (persisted to `./llm_providers.json` with `kind: "custom"`).
API keys are **never** written to disk — only `api_key_env` is stored. Session keys are
passed per request for Test / enrich.

**Enrich UI:** Enrich tab → **+ Add custom** → preset **SGLang + Qwen** → set model from
**Fetch /models** → **Test connection** (3 stages) → **Save profile**.

**CLI:**

```bash
# Prefill SGLang + Qwen defaults
redibis llm add sglang-qwen --preset sglang-qwen \
  --model Qwen/Qwen2.5-14B-Instruct \
  --api-base http://localhost:30000/v1 \
  --param timeout=120 --param max_tokens=4096

# Staged diagnostics (reachability → completion → json_mode)
redibis llm test sglang-qwen --api-key sk-local -v

redibis enrich telecom.customers --provider sglang-qwen
redibis llm remove sglang-qwen
```

Free-form `params` are passed as LiteLLM completion kwargs (temperature, top_p,
max_tokens, timeout, stop, extra_body, …). Reserved keys are stripped: `model`,
`messages`, `api_base`, `api_key`, `response_format`.

Advanced escape hatch: `redibis llm add my-raw --raw --litellm-model openai/foo`.

API routes (same registry as CLI):

| Method | Path | Role |
|--------|------|------|
| GET | `/api/llm/providers` | Merged list + `editable` / presets |
| POST | `/api/llm/providers` | Create/update custom profile |
| DELETE | `/api/llm/providers/{name}` | Delete custom profile |
| POST | `/api/llm/providers/test` | Staged test (saved name or inline payload) |
| GET | `/api/llm/providers/{name}/models` | Proxy `{api_base}/models` (SSRF-guarded) |

Any other OpenAI-compatible gateway (vLLM, LM Studio, llama.cpp, TGI) works the same way.

---

## Gemini (Google cloud)

**1. API key** — create a key in Google AI Studio / Vertex and export:

```bash
export GEMINI_API_KEY=your-key-here
```

The packaged provider reads `api_key_env: GEMINI_API_KEY` (never put keys in `llm_providers.json`).

**2. CLI:**

```bash
redibis enrich telecom.customers --provider gemini
redibis enrich telecom.customers --provider gemini --model gemini-2.5-flash
```

**3. From contract file:**

```bash
redibis enrich -f contract.deterministic.yaml --provider gemini --model gemini-2.5-flash
```

**Python:**

```python
provider = get_provider("gemini", model="gemini-2.5-flash", api_key=os.environ["GEMINI_API_KEY"])
result = enrichment_service_for_store(store).enrich("telecom.customers", provider)
```

**Cloud / residency behaviour:**

- Gemini is treated as a **cloud provider** → default `sample_policy=masked` (no raw cell values in the prompt).
- Upload **masked sample CSV** under the table’s enrichment sample docs in the UI, or use CLI `--external-masked-ack` only after masked data is uploaded.
- RAI middleware runs via `guarded_model_call`; see [`docs/LLM_ENRICHMENT.md`](LLM_ENRICHMENT.md#sample-policy-two-independent-axes).
- Dev bypass (logged): `redibis enrich … --provider gemini --bypass-rai`

**Test connectivity without enriching:**

```bash
redibis llm test gemini
redibis llm test gemini --model gemini-2.5-flash --prompt "Reply with OK"
```

- Web: **Settings → LLM → Test connection** on the `gemini` row.
- REST: `POST /api/llm-providers/gemini/test` with optional `{ "model": "gemini-2.5-flash" }`.

---

## Enrichment CLI examples

Common flags for all providers:

```bash
redibis enrich <table> --provider <name> \
  [--contract|-f contract.yaml] \   # input contract file (optional)
  [--model MODEL] \                 # override default from llm_providers.json
  [--endpoint URL] \                # override api_base (vLLM / Ollama)
  [--api-key KEY] \                 # ephemeral key (else use api_key_env)
  [--providers-file path.json] \
  [--prompt system_prompt.txt] \
  [--instructions "extra guidance"] \
  [--context doc1.md] [--example-docs ex.yaml] \
  [--external-masked-ack] \         # cloud: attest masked samples uploaded
  [--bypass-rai]                    # dev only — skips RAI gate
```

Fetch LLM-call evidence after a run:

```bash
redibis get llm-call-logs <run_id> --zip enrich-evidence.zip
```

Shareable artifacts only. Exact prompts: `redibis scan evidence llm --raw`
([EVIDENCE_STORE.md](EVIDENCE_STORE.md)).

See [`docs/LLM_ENRICHMENT.md`](LLM_ENRICHMENT.md) for architecture, run artifacts, and the shared UI/agent path.

---

## Provider registry

Resolution order (first match wins):

1. `REDIBIS_LLM_PROVIDERS` env var → path to a JSON file
2. `./llm_providers.json` in the current working directory
3. Packaged `providers_default.json`

Example `llm_providers.json`:

```json
{
  "providers": {
    "my_vllm": {
      "description": "Team vLLM endpoint",
      "litellm_model": "hosted_vllm/Qwen/Qwen2.5-7B-Instruct",
      "model_prefix": "hosted_vllm",
      "api_base": "http://vllm.example.com:8000/v1",
      "api_key_env": "VLLM_API_KEY",
      "supports_json": true,
      "params": { "temperature": 0.2 }
    }
  }
}
```

**Never** put API keys in this file for production. Use `api_key_env` and set the env var on the host.

## Settings → LLM — test workbench

1. Open **Settings → LLM**.
2. Each registered provider shows model, endpoint, and whether `api_key_env` is set.
3. Enter an **ephemeral** API key (optional) or rely on the env var.
4. Click **Test connection** — runs a tiny probe (`max_tokens=16`, 20s timeout).
5. Expand **Show provider log** to see redacted LiteLLM debug output on failure.

REST equivalent: `POST /api/llm-providers/{name}/test` with body `{model?, api_key?, endpoint_url?, timeout?}`.

### CLI — `redibis llm list|test`

Same probe as the Settings workbench (shared `redibis.enrich.probe.probe_provider`):

```bash
# List registered providers (local + cloud)
redibis llm list
redibis llm list --json

# Offline path (no network)
redibis llm test demo

# Local servers
redibis llm test ollama
redibis llm test ollama --model qwen2.5 --prompt "Say hello in one word"
redibis llm test vllm --endpoint http://localhost:8001/v1 --model Qwen/Qwen2.5-7B-Instruct
redibis llm test sglang --endpoint http://localhost:30000/v1 --model meta-llama/Llama-3.1-8B-Instruct
redibis llm test slang   # alias for sglang

# Any other OpenAI-compatible server: reuse vllm or sglang + --endpoint
redibis llm test vllm --endpoint http://127.0.0.1:8080/v1 --model my-model

# Cloud (keys from env)
export OPENAI_API_KEY=… ; redibis llm test openai --model gpt-4o-mini
export ANTHROPIC_API_KEY=… ; redibis llm test claude --model claude-haiku-4-5
export GEMINI_API_KEY=… ; redibis llm test gemini --model gemini-2.5-flash

# Ephemeral key / JSON / verbose log
redibis llm test openai --api-key sk-… --json -v

# Probe every registered provider (except demo)
redibis llm test --all
```

Exit code `0` = OK, `1` = at least one failure.

## LLM call debug log

Every `LiteLLMProvider.complete()` emits:

- A structured log line on logger `redibis.llm` (visible in per-run SSE logs during enrichment).
- A `ModelCallRecord` in the in-memory ring buffer (`GET /api/llm/calls?limit=50`).
- A telemetry span when inside `run_context(run_id)`.

Fields: provider, model, latency, tokens, cost, status, api_base (host only), redacted error.

### Observability config

```yaml
observability:
  llm_debug: false        # raise LiteLLM logger to DEBUG per call (redacted in UI)
  llm_log_prompts: false  # log prompt lengths/hashes only; never bodies by default
```

Env overrides: `REDIBIS_LLM_DEBUG=1`, `REDIBIS_LLM_LOG_PROMPTS=1`.

## Offline demo provider

`demo` needs no `litellm` install — use it to verify the UI and logging path without network.

---

## Text Gateway capability roles

The Text Gateway (`/gateway`) and its guards route through the same
capability-role matrix as everything else. Prefer **Settings → Text Gateway**
(dedicated models tab) for the three gateway roles, or the full matrix under
**Settings → LLM → Capability roles**:

| Role | Used by | Notes |
|---|---|---|
| `pii.text_refiner` | Text Gateway "Use LLM refiner", `/api/pii/text/scan` `use_llm` | Free-text span proposals (`is_proposal: true`); distinct from column-shape `pii.refiner`. Resolves from this role first, then legacy `pii.llm`. |
| `gateway.toxicity` | `/api/gateway/scan`, `/api/gateway/guard`, `check_toxicity` | Optional LLM judge behind the always-free heuristic; any LiteLLM model or a dedicated moderation model |
| `gateway.prompt_injection` | same, `check_prompt_injection` | Also reused by the CopilotKit chat bridge before the planner runs |

**Default local runtime for new Gateway bindings is SGLang** (GPU). Also
supported: **vLLM** (GPU) and **Ollama** (CPU or GPU). Use the tab's
**Test connection** button (`POST /api/llm/routes/{role}/test`) before
enabling `use_llm` on a scan. Leaving a role unbound is safe — the refiner
reports unavailable / skipped, and guards fall back to heuristics.

### Free-text PII refiner (`pii.text_refiner`)

Unlike column-shape enrichment (which defaults to `sample_policy=masked`
for cloud providers), the free-text refiner sends **raw pasted text** to
the model — there is no column sample to mask. Because of that, it is
**local-only by default**: `LlmTextRefiner` refuses cloud providers unless
you explicitly opt in. Allowed local names include `sglang`, `vllm`,
`ollama`, `lmstudio`, and related aliases:

```yaml
pii:
  llm:
    enabled: true          # optional if pii.text_refiner is bound in Settings
    provider: sglang
    model_name: Qwen/Qwen2.5-7B-Instruct
    endpoint_url: http://localhost:30000/v1
    # Only set this to send raw pasted text to a *cloud* provider such as
    # gemini/openai/claude. RAI residency policy still applies to the call.
    allow_external_raw_text: false
```

The Text Gateway UI's provider/model selector (populated from `GET
/api/gateway/health`) enforces the same rule per request — selecting a
cloud provider without the flag set returns `403`, an unknown provider name
returns `400`. Every call — local or cloud — still routes through
`guarded_model_call` with `attested_masked_external=False`, so RAI
evaluates it as real raw-text exposure, never as an attested-masked sample.
Arabic / spoken-digit prompting is enabled automatically when
`language` starts with `ar`. LLM spans always remain `is_proposal: true`.

See [`docs/TEXT_PII_SCAN.md`](TEXT_PII_SCAN.md#toxicity-and-prompt-injection-guards)
for the toxicity/prompt-injection guards.
