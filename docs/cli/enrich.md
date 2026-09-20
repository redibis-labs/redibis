# CLI help — enrich (LLM contract enrichment)

Enrich an **active** contract (or a YAML file) with business definitions, optional
PII refinements, and (in multistep mode) an inferred **table description** plus a
final **whole-contract review**. On success the result is **auto-written** to
active storage. Prompt markdown can be exported, customized, and re-imported —
see [Context profiles](#context-profiles).

```bash
pip install -e ".[enrich]" -c requirements/constraints.txt
# Multistep LangGraph path also needs:
pip install -e ".[enrich,agents]" -c requirements/constraints.txt
```

## Enrich active contract (single LLM call — default)

```bash
# Local OpenAI-compatible (vLLM / custom / SGLang)
redibis enrich telecom.customers \
  --provider vllm \
  --endpoint http://127.0.0.1:8080/v1 \
  --model Qwen/Qwen2.5-7B-Instruct \
  --output-dir ./reports

redibis enrich telecom.customers --provider ollama --model qwen2.5 \
  --output-dir ./reports

redibis enrich telecom.customers --provider sglang \
  --endpoint http://localhost:30000/v1 \
  --model Qwen/Qwen2.5-7B-Instruct \
  --output-dir ./reports

# Cloud
export GEMINI_API_KEY=… ; redibis enrich telecom.customers --provider gemini --output-dir ./reports
export ANTHROPIC_API_KEY=… ; redibis enrich telecom.customers --provider claude --output-dir ./reports
export OPENAI_API_KEY=… ; redibis enrich telecom.customers --provider openai --output-dir ./reports
```

## Multistep (LangGraph stages)

How-to including **batch** and **batch + `--multistep`**: [multi-and-batch-enrich.md](multi-and-batch-enrich.md).

Default enrich remains **one** LLM call. Opt into a YAML-defined sequence of
**prebuilt** stages (add / remove / reorder; no arbitrary Python):

| Stage kind | Purpose |
|------------|---------|
| `column_definitions` | Column `business` / `businessName` / semantic tags |
| `classification_pii` | Column `pii` classification + entity overrides |
| `table_definition` | Agent-discoverable table description from evidence |
| `contract_review` | Whole-contract consistency review (bounded corrections + findings) |
| `validate` | ODCS validation only (no LLM) |

```bash
# Built-in default order: columns → privacy → table → review
redibis enrich telecom.customers --provider demo --multistep --output-dir ./reports
redibis enrich run telecom.customers --provider demo --multistep --output-dir ./reports

redibis enrich telecom.customers --provider vllm \
  --multistep --steps-file config/examples/enrich-steps-default.yaml \
  --endpoint http://127.0.0.1:8080/v1 \
  --output-dir ./reports
```

Example steps file (`config/examples/enrich-steps-default.yaml`):

```yaml
version: 1
max_attempts: 3
rai_enabled: null    # null = use redibis.yaml / defaults
steps:
  - id: columns
    kind: column_definitions
    enabled: true
  - id: privacy
    kind: classification_pii
    enabled: true    # set false to skip
  - id: table
    kind: table_definition
    enabled: true
  - id: review
    kind: contract_review
    enabled: true
```

Disable the extra review LLM call without a steps file:

```yaml
enrich:
  multistep:
    review_enabled: false
    review_max_changes: 25
```

### Retries, ODCS gate, RAI

- Each stage validates its delta and the evolving candidate as a **standard ODCS v3** contract.
- On model / parse / ODCS errors, the stage **retries** (default **3** attempts, including the first).
- Override globally with `enrich.max_attempts` or per-step `max_attempts` in the YAML.
- Invalid workflow YAML, unknown step kinds, and missing credentials fail immediately (no retry).
- Intermediate stages use `write_active=False`; **one** active write happens only after the final candidate passes ODCS validation.
- RAI is **optional** for local providers: set `enrich.multistep.rai_enabled: false`, workflow `rai_enabled: false`, or CLI `--bypass-rai`. Output privacy scrubbing still always runs.

```yaml
enrich:
  include_pii_evidence: false
  max_attempts: 3
  context:
    pack: ""
    custom_dir: ""
  multistep:
    enabled: false
    steps_file: config/examples/enrich-steps-default.yaml
    rai_enabled: false   # local LLM
    review_enabled: true
    review_max_changes: 25
  similar_context:
    enabled: true
    k: 5
```

## From a contract file (no prior active)

```bash
redibis enrich --contract ./reports/<run_id>/contract.deterministic.yaml \
  --provider vllm --endpoint http://127.0.0.1:8080/v1 \
  --output-dir ./reports

redibis enrich telecom.customers -f my_contract.yaml --provider ollama \
  --output-dir ./reports
```

## Context & instructions (CLI = UI context bundle)

| Flag | Purpose |
|------|---------|
| `--context FILE …` | Design / company docs uploaded into enrich context |
| `--example-docs FILE …` | Few-shot example documents |
| `--examples TABLE …` | Few-shot contracts already in the store |
| `--prompt FILE` | Replace default system prompt |
| `--instructions TEXT_OR_FILE` | Extra steward guidance |
| `--external-masked-ack` | Attest masked samples for cloud providers |
| `--bypass-rai` | Dev only — skip RAI residency checks (logged) |
| `--multistep` | Run YAML LangGraph stage sequence |
| `--steps-file FILE` | Workflow YAML (prebuilt stages only) |
| `--pack` | Enrichment pack folder, `.zip`, or exported context-profile DIR |
| `--dry-run` | Build prompts only (no LLM, no write) |

```bash
redibis enrich telecom.customers --provider vllm \
  --endpoint http://127.0.0.1:8080/v1 \
  --context docs/domain_glossary.md \
  --instructions "Prefer telecom terms; short definitions." \
  --output-dir ./reports
```

## Evidence after a run

```bash
redibis show telecom.customers --output-dir ./reports
redibis get llm-call-logs <run_id> --zip evidence.zip --output-dir ./reports
```

`get llm-call-logs` is shareable-only: no `evidence_bundle.json`, no `*.raw.json`.
Restricted exact copies: [EVIDENCE_STORE.md](../EVIDENCE_STORE.md).

## Batch enrich (SGLang) — `./scan_output`

How-to (multistep vs batch vs both): **[multi-and-batch-enrich.md](multi-and-batch-enrich.md)**.

`redibis enrich` is **one table or one `--contract` file**. There is no
`enrich-batch`. Prefer the packaged driver (same root as `batch_scan.sh`):

```bash
./scripts/batch_enrich.sh                          # ./scan_output + SGLang
./scripts/batch_enrich.sh -o ./scan_output --push  # then catalog push-batch
./scripts/batch_enrich.sh --from-store             # every table from `redibis list`
```

Probe first: `redibis llm test sglang --endpoint http://localhost:30000/v1`

**Gotcha:** if your scan used a nested `--output-dir` (e.g.
`./scan_output/<workflow>/...`), contracts live under that nested path, not
directly under `./scan_output`. Check with:
`find ./scan_output -type d -path '*active-contracts/active'`.

### Every contract file in `./scan_output` (`active/` only)

Target `active/`, not the whole `active-contracts/` folder — `audit/{table}/{uuid}.yaml`
holds immutable historical snapshots of the **same** table; enriching those
re-processes stale duplicates.

```bash
OUT=./scan_output
DIR="$OUT/_dev_storage/active-contracts/active"

find "$DIR" -type f \( -name '*.yaml' -o -name '*.yml' \) | while read -r f; do
  echo "=== enrich $f ==="
  redibis enrich --contract "$f" \
    --provider sglang \
    --endpoint http://localhost:30000/v1 \
    --model Qwen/Qwen2.5-7B-Instruct \
    --api-key sk-local \
    --output-dir "$OUT" \
    || echo "enrich failed: $f"
done
```

Named provider from `llm_providers.json` (e.g. `sglang-qwen`):

```bash
export REDIBIS_LLM_PROVIDERS=config/examples/llm-providers-sglang-qwen.json
export SGLANG_API_KEY=sk-local

find ./scan_output/_dev_storage/active-contracts/active -type f \( -name '*.yaml' -o -name '*.yml' \) \
  | while read -r f; do
      redibis enrich --contract "$f" \
        --providers-file "$REDIBIS_LLM_PROVIDERS" \
        --provider sglang-qwen \
        --endpoint http://localhost:30000/v1 \
        --api-key sk-local \
        --output-dir ./scan_output \
        || echo "enrich failed: $f"
    done
```

### Every table already in the active store

```bash
OUT=./scan_output
redibis list --output-dir "$OUT" | sed 's/^[[:space:]]*//' | while read -r table; do
  [ -z "$table" ] && continue
  echo "=== enrich $table ==="
  redibis enrich "$table" \
    --provider sglang \
    --endpoint http://localhost:30000/v1 \
    --model Qwen/Qwen2.5-7B-Instruct \
    --api-key sk-local \
    --output-dir "$OUT" \
    || echo "enrich failed: $table"
done
```

Then push the same folder (no `-r` needed — `active/` is flat, one file per table):

```bash
redibis catalog push-batch ./scan_output/_dev_storage/active-contracts/active \
  --backend openmetadata
```

## Then push to OpenMetadata

Enrich writes the active contract only. Publish with `redibis catalog …`
(needs `OM_BOT_JWT` + catalog config). Full flag sheet: [catalog.md](catalog.md).

```bash
export OM_BOT_JWT='…'   # real JWT
export REDIBIS_CONFIG=config/examples/catalog-openmetadata.yaml

# one table already in the active store
redibis catalog push telecom.customers --backend openmetadata --dry-run --json
redibis catalog push telecom.customers --backend openmetadata

# one YAML file
redibis catalog push-file ./path/to/contract.yaml --backend openmetadata

# every contract in ./scan_output (active/ only — skip audit/ duplicates)
redibis catalog push-batch ./scan_output/_dev_storage/active-contracts/active \
  --backend openmetadata
```

Air-gap cookbook (enrich + push-file + push-batch + scan folder):
`enterprise/docs/install/enrich-and-catalog.md`.

Look for table description under `schema[0].description` after one-call enrich
or a multistep run with `table_definition` enabled. Whole-contract review findings land in
`enrichment_meta.review_findings` and are never written into the ODCS contract.

## Context profiles

`redibis enrich TABLE` stays the run command. `redibis enrich run TABLE` is the
unambiguous form when a table name would collide with a reserved word
(`run`, `export-context`, `add-context`, `list-context`, `remove-context`).

Prompt markdown is composed by **mode**:

| Mode | What the LLM sees |
|------|-------------------|
| one-call (`redibis enrich TABLE`) | `normal/*.md` |
| `--multistep` | `multistep/shared/*.md` + `multistep/steps/<kind>/*.md` for the current stage |

Legacy flat files under `configs/prompts/enrich/*.md` remain valid and count as
**shared** when nested folders are absent.

Composition order (later layers win on the same relative path):

1. Shipped builtins / applied `.rdbpack` prompt store
2. Configured `enrich.context.pack` or CLI `--pack`
3. Global custom overlays (`$REDIBIS_CONFIGS_DIR/enrich-context/custom/`)
4. Table-scoped overlays (`enrichment/{table}/context/…`)
5. Per-run `--instructions` / `--context`

### Export / re-import

```bash
# Resolved effective profile (not table-rendered prompts)
redibis enrich export-context ./context-profile --output-dir ./reports

# From an enterprise/default .rdbpack or enrichment pack folder
redibis enrich export-context ./context-profile --pack ./enterprise.rdbpack

# Include operator-uploaded global overlays (off by default — can be sensitive)
redibis enrich export-context ./context-profile --include-custom

# The export is itself a valid --pack DIR (round-trip)
redibis enrich telecom.customers --pack ./context-profile --provider demo --dry-run \
  --output-dir ./reports
```

Export layout:

```text
DIR/
  context-manifest.yaml   # checksums, source pack, layer order, stage kinds
  manifest.yaml           # enrichment pack v1 — valid --pack DIR
  normal/*.md
  multistep/
    shared/*.md
    steps/column_definitions/*.md
    steps/classification_pii/*.md
    steps/table_definition/*.md
    steps/contract_review/*.md
```

### Add / list / remove overlays

```bash
# Global (all tables, both modes)
redibis enrich add-context glossary.md --scope global --mode shared \
  --output-dir ./reports

# One-call enrich only
redibis enrich add-context steward-voice.md --scope global --mode normal \
  --output-dir ./reports

# One multistep stage only
redibis enrich add-context pii-policy.md --scope global \
  --mode multistep --stage classification_pii --output-dir ./reports

# Table-scoped (this table only)
redibis enrich add-context table-notes.md --scope table \
  --table telecom.customers --mode normal --output-dir ./reports

redibis enrich list-context --scope global --json --output-dir ./reports
redibis enrich list-context --scope table --table telecom.customers --output-dir ./reports

redibis enrich remove-context shared/glossary.md --scope global --output-dir ./reports
redibis enrich remove-context table-notes.md --scope table \
  --table telecom.customers --output-dir ./reports
```

`--mode` is `shared` | `normal` | `multistep`. `--stage` is only valid with
`--mode multistep` and must be a prebuilt LLM stage kind
(`column_definitions`, `classification_pii`, `table_definition`, `contract_review`).

Per-run `--context FILE` still uploads into the table's shared context (same as
before) and applies to both modes.

### Config

```yaml
enrich:
  context:
    pack: ./context-profile          # or an enrichment pack / .rdbpack
    custom_dir: ""                   # default: $REDIBIS_CONFIGS_DIR/enrich-context/custom
  multistep:
    enabled: false                   # true = --multistep without the CLI flag
    review_enabled: true
    review_max_changes: 25
```

## See also

- [catalog.md](catalog.md) — push / push-file / push-batch / delete / wipe
- [multi-and-batch-enrich.md](multi-and-batch-enrich.md) — how to run `--multistep`, `batch_enrich.sh`, and both
- `scripts/batch_enrich.sh` — batch enrich under `./scan_output`
- [llm.md](llm.md) — test connectivity before enriching
- [catalog.md](catalog.md) — push enriched contracts to OpenMetadata
- [../LLM_PROVIDERS.md](../LLM_PROVIDERS.md) — vLLM / Ollama / SGLang / Gemini setup
- [../LLM_ENRICHMENT.md](../LLM_ENRICHMENT.md) — architecture
- [steward.md](steward.md) — `--context-pack` from Finalize A3 `llm_context/`;
  `--steward-verdict-path` locks human-verified PII columns so enrich skips them
