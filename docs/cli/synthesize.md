# CLI — Contract Synthesis

Build a **portable ODCS v3.1.0** candidate from an existing ODCS v3 base contract
plus requirement documents and Spark / SQL / DataStage sources.

This path is **out of merge**: it does **not** call `ContractStore.upsert()` and
does not change Redibis active contracts. Existing scan / enrich / store workflows
stay on their current emit version (`v3.0.1`).

## Quick start

```bash
redibis contract synthesize \
  -f tests/fixtures/synthesis/cafc/02_data_contract.odcs.yaml \
  --requirements tests/fixtures/synthesis/cafc/requirements \
  --source tests/fixtures/synthesis/cafc/sources \
  --analysis-mode deterministic \
  --odcs-version v3.1.0 \
  -o ./synthesis_out/cafc
```

Outputs under `-o` include:

- `contract.synthesized.v3.1.yaml` / `.json` — portable candidate
- `lineage.json` + `lineage_graph.json` — transformation lineage (not FK-only)
- `requirements_traceability.json` / `.md` — requirement coverage matrix
- `evidence_bundle.json`, `stage_results.json`, `synthesis_meta.json`

## Analysis modes

| Mode | Behavior |
|------|----------|
| `deterministic` (default) | No model calls; unresolved prose stays unresolved |
| `assisted` | Sends only unresolved/conflicting evidence + bounded excerpts through `guarded_model_call` |

```bash
redibis contract synthesize -f base.yaml --inputs docs.zip \
  --analysis-mode assisted --provider demo --compare-modes -o ./out
```

`--compare-modes` also runs assisted against the deterministic snapshot and writes a
side-by-side comparison artifact when a provider is available.

## Inputs

Accepted: `.md` / `.txt` / `.yaml` / `.json` requirements; `.sql`; Spark `.py` / `.scala`;
DataStage `.dsx` / `.xml`; ZIP archives of the above; directories of allowed files.

Rejected: binary / executable packages (`.jar`, `.dstx`, …), ZIP path traversal,
NUL-binary payloads. Uploaded code is **never executed** (SQL/Spark/DataStage are
parsed as text / AST only).

## Version policy

- Synthesis target: **ODCS v3.1.0** (approved Bitol schema bundled in-tree).
- **ODCS v3.2.0** adapter slot exists but is **disabled** until Bitol publishes the
  finalized schema.
- Active Redibis writers continue to emit `v3.0.1`; converting an active contract to
  v3.1 happens only in this portable export path.

## Dashboard / API / agent

- Dashboard tab **Synthesis** (contract detail): upload requirements/sources, run,
  review coverage + lineage edges (`reads_from` / `transforms_to` / `joins_on` /
  `foreign_key` / `lookup`).
- `POST /api/synthesis/{table}/run` — base = active store contract; portable artifacts only.
- `POST /api/synthesis/run-file` — upload base contract file.
- Agent node `contract_synthesis` — distinct from `enrich`; never upserts.

## See also

- [contract.md](contract.md) — inspect / export active contracts
- [enrich.md](enrich.md) — LLM business/PII enrich (bounded delta; not full-schema rewrite)
- [../agents/AGENTIC_GOVERNANCE_DESIGN.md](../agents/AGENTIC_GOVERNANCE_DESIGN.md)
