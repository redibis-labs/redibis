# Telco Enrichment Lite (reference pack)

Bounded demo pack for Redibis enrichment. Useful for `pack validate`,
`pack inspect`, and dry-run enrichment demos.

For a **distribution-ready golden pack** (25 glossary entries, 3 examples,
evals, LICENSE, ZIP builder), see:

- Folder: `dist/enrichment_packs/telco-enrichment-golden/`
- Zip: `dist/enrichment_packs/telco-enrichment-golden-1.0.0.zip`

```bash
redibis pack validate examples/enrichment_packs/telco
redibis pack inspect examples/enrichment_packs/telco
redibis enrich --contract examples/enrichment_packs/telco/examples/customer-input.yaml \
  --pack examples/enrichment_packs/telco --provider demo --dry-run

# Optional: export the resolved prompt profile (normal + multistep folders)
redibis enrich export-context ./context-profile --pack examples/enrichment_packs/telco
```
