# Codegen (open-core)

Open-core Redibis ships **local** codegen only:

- Deterministic Ranger template via `redibis.agents.codegen_local`
- Optional local LLM (Ollama / vLLM / SGLang / demo) through `guarded_model_call`
- Thin remote client (`redibis.agents.codegen_remote_client`) when you have a
  vendor-issued URL + `REDIBIS_CODEGEN_TOKEN`

```python
from redibis.agents.codegen import submit_codegen

result = submit_codegen(
    intent="Mask PII columns",
    table="telecom.customers",
    contract=contract,
    target_system="ranger",
)
# result["status"] == "proposed" — never executed by open-core
```

## Configuration

```yaml
agents:
  codegen_mode: local          # or "remote"
  codegen_service_url: ""      # required for remote
  # REDIBIS_CODEGEN_TOKEN must be set in the environment for remote
```

## Hosted codegen

A vendor-hosted codegen endpoint (URL + token) is optional. Open-core never
ships the hosted service package — customers only configure the remote URL and
token. Contact your vendor for onboarding; air-gapped sites should stay on
`codegen_mode: local`.
