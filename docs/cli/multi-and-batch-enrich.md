# Multistep enrich and batch enrich

Two different operations: **multistep** runs several LLM stages on **one** contract;
**batch** runs enrich on **many** contracts. Combine them for “batch multi enrich”.

There is **no** `redibis enrich-batch` command. Full flag list: [enrich.md](enrich.md).
Architecture: [LLM_ENRICHMENT.md](../LLM_ENRICHMENT.md).

## Install

```bash
pip install -e ".[enrich]" -c requirements/constraints.txt
# LangGraph path (otherwise sequential fallback):
pip install -e ".[enrich,agents]" -c requirements/constraints.txt
```

Contracts must already exist (scan with `--automerge` / `--auto-write`, or pass
`--contract FILE`). Probe the LLM first:

```bash
redibis llm test sglang --endpoint http://localhost:30000/v1
```

---

## 1. Multistep enrich (one table, many stages)

Default `redibis enrich` is **one** LLM call. `--multistep` runs a YAML sequence of
prebuilt stages, then **one** active write if the final candidate is ODCS-valid.

| Stage | What it fills |
|-------|----------------|
| `column_definitions` | Column business names / definitions / tags |
| `classification_pii` | PII classification + entity overrides |
| `table_definition` | Table description (`schema[0].description`) |
| `contract_review` | Whole-contract consistency (bounded edits) |
| `validate` | ODCS check only (no LLM) |

Default order: columns → privacy → table → review.

```bash
# demo / smoke
redibis enrich telecom.customers --provider demo --multistep --output-dir ./reports

# production (vLLM / SGLang) + default stage YAML
redibis enrich telecom.customers --provider vllm \
  --multistep --steps-file config/examples/enrich-steps-default.yaml \
  --endpoint http://127.0.0.1:8080/v1 \
  --output-dir ./reports
```

File that is not yet in the store:

```bash
redibis enrich --contract ./reports/<run_id>/contract.deterministic.yaml \
  --provider sglang --multistep --output-dir ./scan_output
```

Enable without the CLI flag (YAML):

```yaml
enrich:
  multistep:
    enabled: true
    steps_file: config/examples/enrich-steps-default.yaml
    review_enabled: true
    review_max_changes: 25
    rai_enabled: false   # typical for local LLMs
```

Skip the extra review LLM call with `enrich.multistep.review_enabled: false`.
Local providers may use `--bypass-rai` (logged). Output privacy scrubbing always runs.

---

## 2. Batch enrich (many tables)

Use `./scripts/batch_enrich.sh`. It walks
`./scan_output/_dev_storage/active-contracts/active` (same root as `batch_scan.sh`)
and calls `redibis enrich` once per YAML. Only **`active/`** — do not walk `audit/`
(those are old snapshots of the same tables).

```bash
# default: ./scan_output + SGLang
./scripts/batch_enrich.sh

./scripts/batch_enrich.sh -o ./scan_output --provider sglang
./scripts/batch_enrich.sh -o ./scan_output --provider sglang --push   # then catalog push-batch
./scripts/batch_enrich.sh --from-store --provider sglang              # every table from `redibis list`
```

| Flag | Default | Purpose |
|------|---------|---------|
| `-o, --output` | `./scan_output` | Scan / artifact root (`--output-dir`) |
| `--contracts-dir` | `OUTPUT/_dev_storage/active-contracts/active` | YAML folder to walk |
| `--from-store` | off | Enrich every table from `redibis list` |
| `--provider` | `sglang` | LLM provider |
| `--endpoint` | `http://localhost:30000/v1` | OpenAI-compatible base URL |
| `--model` | `Qwen/Qwen2.5-7B-Instruct` | Model override |
| `--api-key` | `$SGLANG_API_KEY` or `sk-local` | Provider key |
| `--providers-file` | `$REDIBIS_LLM_PROVIDERS` | `llm_providers.json` |
| `--config` | `$REDIBIS_CONFIG` | `redibis.yaml` |
| `--push` | off | After enrich, `catalog push-batch` that folder |
| `--fail-fast` | off | Stop on first enrich failure |

If scans used a nested `--output-dir`, pass that nested path or `--contracts-dir`:

```bash
find ./scan_output -type d -path '*active-contracts/active'
./scripts/batch_enrich.sh --contracts-dir ./scan_output/<workflow>/_dev_storage/active-contracts/active
```

---

## 3. Batch **and** multistep together

The batch script forwards leftover args to `redibis enrich` (after `--` or as
unknown flags):

```bash
./scripts/batch_enrich.sh -o ./scan_output --provider sglang \
  -- --multistep --steps-file config/examples/enrich-steps-default.yaml

# or set enrich.multistep.enabled: true in YAML
./scripts/batch_enrich.sh -o ./scan_output --provider sglang --config ./redibis.yaml
```

That is batch multi enrich: every active contract, each run through the
multistep pipeline.

Manual loop (same idea):

```bash
DIR=./scan_output/_dev_storage/active-contracts/active
find "$DIR" -type f \( -name '*.yaml' -o -name '*.yml' \) | while read -r f; do
  redibis enrich --contract "$f" \
    --provider sglang --endpoint http://localhost:30000/v1 \
    --multistep --steps-file config/examples/enrich-steps-default.yaml \
    --output-dir ./scan_output \
    || echo "enrich failed: $f"
done
```

Then push the same flat `active/` folder:

```bash
redibis catalog push-batch ./scan_output/_dev_storage/active-contracts/active \
  --backend openmetadata
```

---

## Gotchas

- Nested scan output is easy to miss — contracts may live under
  `./scan_output/<workflow>/_dev_storage/…`, not `./scan_output/_dev_storage/…`.
- Table description appears under `schema[0].description` after one-call enrich
  or a multistep run with `table_definition` enabled.
- Review findings land in `enrichment_meta.review_findings`, not in the ODCS
  contract document.

## See also

- [enrich.md](enrich.md) — flags, context profiles, single-table examples
- [config-batch.md](config-batch.md) — `redibis.yaml` knobs + batch scan
- [catalog.md](catalog.md) — `push` / `push-file` / `push-batch`
- [llm.md](llm.md) — probe connectivity before enriching
- `scripts/batch_enrich.sh` — packaged driver for batch enrich under `./scan_output`
- Air-gap cook: `enterprise/docs/install/enrich-and-catalog.md`
