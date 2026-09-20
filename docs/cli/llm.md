# CLI help — `redibis llm` (list / test / add providers)

Probe LLM connectivity **without** enriching a contract. Same probe as
**Settings → LLM → Test connection** in the UI.

```bash
pip install -e ".[enrich]" -c requirements/constraints.txt
```

## List providers

```bash
redibis llm list
redibis llm list --json
```

Built-in names include: `demo`, `ollama`, `vllm`, `sglang` (alias `slang`),
`openai`, `claude`, `gemini`, `openrouter`, plus any custom profiles you add.

## Test request / response

```bash
# Offline (no network)
redibis llm test demo

# Local
redibis llm test ollama
redibis llm test ollama --model qwen2.5 --prompt "Say hello"
redibis llm test vllm --endpoint http://localhost:8001/v1 --model Qwen/Qwen2.5-7B-Instruct
redibis llm test sglang --endpoint http://localhost:30000/v1 --model Qwen/Qwen2.5-7B-Instruct
redibis llm test slang   # alias → sglang

# Any other OpenAI-compatible server: reuse vllm or sglang + --endpoint
redibis llm test vllm --endpoint http://127.0.0.1:8080/v1 --model my-model

# Cloud (keys from env)
export OPENAI_API_KEY=…    ; redibis llm test openai --model gpt-4o-mini
export ANTHROPIC_API_KEY=… ; redibis llm test claude
export GEMINI_API_KEY=…    ; redibis llm test gemini

# Flags
redibis llm test openai --api-key sk-… --json -v
redibis llm test --all                    # every registered provider except demo
redibis llm test sglang --staged -v       # models → completion → json_mode stages
```

Exit code `0` = OK, `1` = failure. `-v` prints a redacted provider log.

## Add a custom OpenAI-compatible profile

```bash
# Interactive-style one-liner (writes ./llm_providers.json)
redibis llm add my-sglang \
  --api-base http://localhost:30000/v1 \
  --model Qwen/Qwen2.5-7B-Instruct \
  --description "Local SGLang Qwen"

# SGLang + Qwen preset
redibis llm add local-qwen --preset sglang-qwen

# Servers that reject JSON response_format
redibis llm add my-sglang --api-base http://localhost:30000/v1 \
  --model Qwen/Qwen2.5-7B-Instruct --no-json

redibis llm test my-sglang
redibis llm remove my-sglang
```

Never put API keys in the JSON file — use `--api-key-env MY_KEY_ENV` and
`export MY_KEY_ENV=…`, or pass `--api-key` only for a one-off test.

## Capability roles

```bash
redibis llm roles list
redibis llm roles show enrich
redibis llm roles test enrich
```

## UI parity

Settings → LLM and the Enrich page load the same registry (`/api/llm-providers`).
Custom profiles and `sglang` appear in the provider dropdown after reload.

## See also

- [enrich.md](enrich.md) — run enrichment after a successful probe
- [../LLM_PROVIDERS.md](../LLM_PROVIDERS.md) — vLLM / Ollama / SGLang / Gemini detail
