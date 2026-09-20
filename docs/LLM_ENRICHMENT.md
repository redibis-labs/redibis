# LLM enrichment — architecture and usage

Full-contract LLM enrichment adds business definitions, steward-reviewed PII verdicts, and
(optionally) an inferred **table description** to an active ODCS v3 contract. The LLM returns a
**strict delta** (business + optional PII / table edits only); `EnrichmentService` merges it,
scrubs output leaks, and auto-writes on validation success.

Default path: **one** LLM call via `build_context()` → `enrich()`.
Optional path: **`--multistep`** runs a YAML-defined LangGraph (or sequential) sequence of
prebuilt stages with retries, then **one** final active write.

## Shared path

```
UI / CLI / agentic node
        │
        ▼
enrichment_service_for_store(store)   # wires memory retriever
        │
        ├─ single-shot ─► EnrichmentService.enrich(…)
        │
        └─ --multistep ─► MultistepEnrichmentRunner (workflow YAML)
                │   stages: column_definitions → classification_pii → table_definition → contract_review
                │   per-stage RAI (optional) + ODCS validate + retry (default 3)
                ▼
           one active upsert when final candidate is ODCS-valid
```

Key modules:

| Module | Role |
|--------|------|
| `redibis/enrich/service.py` | `EnrichmentService`, `build_context`, `enrich`, `apply_enrichment` |
| `redibis/enrich/workflow.py` | YAML workflow parse / prebuilt stage kinds |
| `redibis/enrich/multistep.py` | LangGraph / sequential runner, retries, final write |
| `redibis/enrich/delta_schema.py` | Delta validation (`table.description` required by prompt; optional in schema) |
| `redibis/enrich/evidence.py` | Per-column context assembly (`build_full_column_context`) |
| `redibis/enrich/pack_digest.py` | Classification pack digest for the system prompt |
| `redibis/enrich/context.py` | UI context bundle (delegates to `build_context`) |
| `redibis/enrich/context_profile.py` | Exportable normal/multistep prompt profiles + custom overlays |
| `redibis/contracts/privacy.py` | `scrub_pii_enrichment_output`, `strip_quality_from_pii_columns` |

Factory helper (CLI, web, agent):

```python
from redibis.enrich.service import enrichment_service_for_store

svc = enrichment_service_for_store(contract_store)
ctx = svc.build_context("telecom.customers", provider)
result = svc.enrich("telecom.customers", provider, enrichment_context=ctx)
```

## Entry points

| Surface | How it calls enrich |
|---------|---------------------|
| **Web** | `POST /api/contracts/{table}/enrich` or `POST /api/enrich/{table}/run` (session + CSRF; [`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl)) |
| **CLI** | `redibis enrich <table> --provider …` · optional `--multistep --steps-file …` · `export-context` / `add-context` |
| **Agent** | Pipeline node `kind=enrich` → `tool_runner._run_enrich` |

**Provider setup (vLLM, Ollama, Gemini):** [`docs/LLM_PROVIDERS.md`](LLM_PROVIDERS.md#quick-reference--vllm-ollama-gemini).
**CLI cheat sheet:** [`docs/cli/enrich.md`](cli/enrich.md).

### CLI examples

Active contract in the store:

```bash
redibis enrich telecom.customers --provider vllm
redibis enrich telecom.customers --provider ollama --model llama3.3
redibis enrich telecom.customers --provider gemini --model gemini-2.5-flash

# Multistep (requires redibis[agents] for LangGraph; sequential fallback if missing)
redibis enrich telecom.customers --provider vllm --multistep \
  --steps-file config/examples/enrich-steps-default.yaml

# Export / customize prompt context (see Context profiles below)
redibis enrich export-context ./context-profile
redibis enrich add-context glossary.md --scope global --mode shared
```

From a YAML/JSON contract file (table resolved from `physicalName` unless you pass `<table>`):

```bash
redibis enrich --contract contract.deterministic.yaml --provider vllm
redibis enrich telecom.customers -f my_contract.yaml --provider ollama
```

Programmatic input contract:

```python
from redibis.enrich.context import load_contract_for_enrichment
from redibis.enrich.providers import get_provider
from redibis.enrich.service import enrichment_service_for_store

contract, table = load_contract_for_enrichment("contract.yaml")
provider = get_provider("vllm", endpoint_url="http://localhost:8001/v1")
result = enrichment_service_for_store(store).enrich(table, provider, input_contract=contract)
```

Preview (no LLM call): `POST /api/enrich/{table}/context/preview` → `preview_enrichment_prompt()` → `build_context()`.

Context bundle assembly: `GET /api/enrich/{table}/context` → `assemble_enrichment_context()` (uses `build_context` for fresh bundles; saved drafts round-trip steward edits).

## Per-column evidence (LLM input)

Each column in `contract_for_prompt.columns` includes:

```yaml
name: merchant_mobile
logicalType: string
physicalType: string
deterministic_verdict:
  entity_type: PHONE_NUMBER
  classification: pii_personal
  tags: [pii, gdpr_personal_data]
  decision_rule: "regex >= 0.80"
engine_evidence:
  regex_hits: [{pattern_name: msisdn_egypt_any, score: 0.97, match_rate: 0.96}]
  ner_hits: []
  phone:
    valid_rate: 0.96
    mobile_rate: 0.94
    regions: {EG: 96}
profiling:
  null_rate: 0.0
  unique_ratio: 0.99
similar_columns: []          # always present; populated when memory enabled
sample: ["0100…", "0111…"]   # local LLM only (see sample policy)
sample_policy: raw
```

Sources: column telemetry (`ContractMetadataStore`), run-bucket `pii_detections.json`, contract quality rules (structural stats only), memory retriever (similar columns).

**Phone evidence:** when `pii.use_phonenumbers` is on and the union gate runs, `engine_evidence.phone`
includes libphonenumber column stats (`valid_rate`, `mobile_rate`, `regions`). A `PHONE_NUMBER`
deterministic verdict requires `valid_rate ≥ pii.msisdn_valid_rate_min` (default 0.80) — regex alone
does not suffice. See `docs/PII_DETECTION_TUNING.md`.

## Sample policy (two independent axes)

**Axis 1 — INPUT (what the LLM reads):**

| Policy | When | Column `sample` |
|--------|------|-----------------|
| `raw` | Local providers (default) | Raw values from telemetry / bundle |
| `masked` | Cloud providers or `--external-masked-ack` | Uploaded masked CSV only |
| `none` | Explicit opt-in (API/programmatic) | Omitted |

Cloud detection uses provider name ∈ `{claude, gemini, openai, openrouter}`.

**Axis 2 — OUTPUT (what lands in the contract):** always personal-data-free, enforced in code:

- `scrub_pii_enrichment_output()` — skeleton `example_values`, redact emails/phones/IDs in definitions and synonyms
- `strip_pii_quality=True` on enrichment upsert — removes value-bearing quality rules from PII columns
- Legacy `merge_candidate()` applies the same scrub + strip

## LLM output (delta only)

The system prompt instructs JSON:

```json
{
  "table": {
    "description": "Optional agent-discoverable table narrative (multistep table_definition).",
    "purpose": "optional short purpose"
  },
  "table_tags": ["telecom"],
  "columns": {
    "merchant_mobile": {
      "businessName": "Merchant mobile number",
      "business": {
        "definition": "…",
        "synonyms": ["mobile", "msisdn"],
        "example_values": ["+20 1# ### ####"],
        "tags": ["contact"]
      },
      "pii": {
        "classification": "pii_personal",
        "entity_type": "PHONE_NUMBER",
        "reason": "Optional one-line override rationale"
      }
    }
  }
}
```

`table.description` is written to `schema[0].description` (and synchronized with the supported
top-level description representation). One-call enrich **must** include `table`; the
`table_definition` multistep stage focuses on it using column defs + profile evidence.

`pii.reason` is persisted to column telemetry as `llm_pii_reason` after a successful write.

The LLM must **not** re-emit schema, quality, telemetry, or full contract YAML.

## Multistep workflow

Prebuilt stage kinds (YAML may enable/disable/reorder; unknown kinds are rejected):

| Kind | Allowed delta fields |
|------|----------------------|
| `column_definitions` | `columns[].business`, `businessName`, semantic `tags` |
| `classification_pii` | `columns[].pii`, governance tags |
| `table_definition` | `table.description` / `purpose`, `table_tags` |
| `contract_review` | union of the above; empty delta means no findings |
| `validate` | no LLM — ODCS assert only |

`contract_review` may apply bounded corrections and records `review_findings` on the stage
record / `enrichment_meta`. The `review` block is stripped before `apply_enrichment`. Disable
with `enrich.multistep.review_enabled: false`. Cap accepted edits with
`enrich.multistep.review_max_changes` (default 25).

Retries: `enrich.max_attempts` (default **3**) or per-step `max_attempts`. Each attempt that
fails ODCS / parse / provider errors feeds a repair prompt. Exhaustion fails closed (no active
write). RAI: optional via `enrich.multistep.rai_enabled` / workflow `rai_enabled` / `--bypass-rai`
for local LLMs; scrubbing always runs.

Example: `config/examples/enrich-steps-default.yaml`.

## Classification pack digest

`build_classification_pack_digest(pack_name)` summarizes the active policy pack (tag taxonomy,
entity→classification mapping, co-tag rules, multi-classification policy) and is appended to the
system prompt under `# CLASSIFICATION PACK DIGEST`.

Pack name: `classification.policy_pack` in YAML (default `telecom`), overridable per context bundle
via `instructions.policy_pack`.

## Configuration

```yaml
enrich:
  include_pii_evidence: false   # regex catalog + NER inventory in user prompt
  max_attempts: 3               # repair retries for multistep (and stage defaults)
  context:
    pack: ""                    # optional enrichment pack / exported profile
    custom_dir: ""              # override global overlay directory
  multistep:
    steps_file: config/examples/enrich-steps-default.yaml
    rai_enabled: false          # optional for local providers
    review_enabled: true
    review_max_changes: 25
  similar_context:
    enabled: true
    k: 5

memory:
  enabled: true                 # required for similar_columns + memory section

classification:
  policy_pack: telecom
```

Context bundle overrides: `global.similar_context.enabled`, `global.sample_policy`.

Agent node: `use_memory: false` disables memory section and per-column `similar_columns`.

## Run artifacts

Each enrich run (via `RunOutputWriter`, workflow=`enrich`) may include:

- `llm_prompt_context.json` — sanitized prompt metadata + `contract_for_prompt`
  (fail-closed if contract sanitization fails; free-text PII omitted)
- `contract.deterministic.yaml` — snapshot before LLM
- `contract.llm.yaml`, `contract_diff.*`, `enrichment_meta.json` — after merge
- `{table}.debug.log` — validation / ODCS summary
- Multistep: `enrichment_meta.stages[]` with per-attempt ok/errors
- Multistep review: `enrichment_meta.review_findings` (never stored on the contract)

Shareable bundle: `redibis get llm-call-logs <run_id> --zip evidence.zip`

That zip never includes `evidence_bundle.json` or `*.raw.json`. Exact prompts
live only under `evidence.restricted_spool_dir` and are read with
`redibis scan evidence llm --raw --actor … --reason …`. See
[EVIDENCE_STORE.md](EVIDENCE_STORE.md).

## Context profiles

Prompt markdown is composed by mode. One-call enrich uses `normal/`; `--multistep`
uses `multistep/shared/` plus `multistep/steps/<kind>/` for the current stage only
(`filter_delta_for_stage()` still enforces the allowed delta). Legacy flat
`prompts/enrich/*.md` files remain valid shared inputs when nested folders are
absent.

```text
configs/prompts/enrich/
  01_role.md                         # legacy flat (shared compatibility)
  normal/*.md                        # one-call enrich
  multistep/shared/*.md
  multistep/steps/column_definitions/*.md
  multistep/steps/classification_pii/*.md
  multistep/steps/table_definition/*.md
  multistep/steps/contract_review/*.md
```

```bash
redibis enrich export-context ./context-profile
redibis enrich export-context ./context-profile --pack ./enterprise.rdbpack --include-custom
redibis enrich add-context notes.md --scope global --mode shared
redibis enrich add-context pii-policy.md --scope global --mode multistep --stage classification_pii
redibis enrich add-context table-notes.md --scope table --table telecom.customers --mode normal
redibis enrich list-context --scope global --json
redibis enrich remove-context shared/notes.md --scope global

# Round-trip: exported DIR is a valid --pack
redibis enrich telecom.customers --pack ./context-profile --provider demo --dry-run
```

The export writes `context-manifest.yaml` (checksums, sources, stage kinds) and
`manifest.yaml` (enrichment pack v1). Custom global overlays live under
`$REDIBIS_CONFIGS_DIR/enrich-context/custom/` and are omitted from export unless
`--include-custom`. Settings → Prompts edits the same nested files.

Layer order: pack / prompt store → global overlays → table overlays → `--instructions`.
Signed / default / enterprise pack files are never rewritten; overlays sit on top.

## Tests

`tests/test_enrich_overhaul.py` — shared context, evidence shape, output scrub, pack digest, CLI/agent parity, bundle alignment, legacy merge.

Related: `tests/test_enrich_context.py`, `tests/test_v2_enrich.py`, `tests/test_enrich_rai_gate.py`,
`tests/test_enrich_workflow.py`, `tests/test_enrich_context_profile.py`, `tests/test_memory_phase4.py`.

## Related docs

- [`docs/cli/enrich.md`](cli/enrich.md) — CLI flags, review stage, context profiles
- [`docs/cli/multi-and-batch-enrich.md`](cli/multi-and-batch-enrich.md) — `--multistep`, `batch_enrich.sh`, and combining them
- [`docs/ENRICHMENT_PACK_AUTHORING.md`](ENRICHMENT_PACK_AUTHORING.md) — optional `prompt.normal` / `shared` / `stages`
- [`docs/cli/catalog.md`](cli/catalog.md) — push enriched contracts to OM
- [`docs/LLM_PROVIDERS.md`](LLM_PROVIDERS.md) — vLLM / Ollama / Gemini setup, provider registry
- [`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md) — `include_pii_evidence` flag
- [`docs/tutorials/CATALOG_OPENMETADATA_TUTORIAL.md`](tutorials/CATALOG_OPENMETADATA_TUTORIAL.md) — catalog push-scan
