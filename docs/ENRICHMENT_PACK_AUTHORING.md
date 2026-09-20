# Enrichment Pack Authoring (v1)

This document covers the **format and correctness requirements** for Redibis
enrichment packs. Use the course for curation methodology; use this doc for
portable pack structure.

## Quick start

```bash
redibis pack validate ./my-pack
redibis pack inspect ./my-pack
redibis enrich --contract contract.yaml --pack ./my-pack --provider demo --dry-run

# Export the resolved prompt profile (normal + per-stage multistep) as a pack DIR
redibis enrich export-context ./context-profile
redibis enrich --contract contract.yaml --pack ./context-profile --provider demo --dry-run
```

Reference lite pack: `examples/enrichment_packs/telco/`

## Layout

```text
my-pack/
├── manifest.yaml          # required
├── README.md              # optional metadata
├── prompts/               # domain.md, terminology.md, edge-cases.md
├── context/               # company.md, glossary YAML, …
├── examples/              # input ODCS + expected deltas
└── evals/cases.yaml       # deterministic assertions
```

Canonical identity is `publisherId/name@version`. `vendor` is display-only.

## Optional mode / stage prompt assets

v1 manifests stay valid with only `prompt.domain` / `terminology` / `edgeCases`.
You may also declare nested profile files so one-call and multistep enrich load
different markdown:

```yaml
prompt:
  domain: prompts/domain.md
  terminology: prompts/terminology.md
  edgeCases: prompts/edge-cases.md
  normal:
    - normal/01_role.md
  shared:
    - multistep/shared/01_role.md
  stages:
    column_definitions:
      - multistep/steps/column_definitions/02_business_definitions.md
    classification_pii:
      - multistep/steps/classification_pii/04_pii_review.md
    table_definition:
      - multistep/steps/table_definition/07_table_definition.md
    contract_review:
      - multistep/steps/contract_review/08_contract_review.md
```

`redibis enrich export-context DIR` writes this layout (plus `context-manifest.yaml`)
so the directory is a valid `--pack DIR`. Custom overlays are added with
`redibis enrich add-context` — see [cli/enrich.md](cli/enrich.md#context-profiles).

## Safety rules

- Packs are Markdown/YAML/JSON only — no Python, no model weights.
- Core Redibis safety and ODCS delta output rules cannot be overridden by pack text.
- Any context omission/truncation requires an explained reduction plan and exact
  `--approve-context-reduction crp_v1_...` approval (interactive CLI may ask).
- `pack inspect` shows metadata and counts; use `--show-content` only when you
  already possess the pack and need local details.

## Related

- Implementation plan: `docs/ENRICHMENT_PACK_V1_IMPLEMENTATION_PLAN.md`
- Classification policy packs are separate (`redibis classify …`).
- Future NER packs (`redibis.ner/v1`) are out of scope for enrichment packs.
