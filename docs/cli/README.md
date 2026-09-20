# CLI help index

Operator help for the **`redibis`** command line. Each page is a copy-paste cheat sheet;
full tours and architecture live in the linked deep dives.

| Topic | Help page | Deep dive |
|-------|-----------|-----------|
| **Scan → active contract** | [scan.md](scan.md) | [CLI_SCAN_ENRICH_TOUR.md](../CLI_SCAN_ENRICH_TOUR.md) |
| **Steward review / A0–A5 / scan memory** | [steward.md](steward.md) | [STEWARD_REVIEW_ARTIFACTS.md](../tutorials/STEWARD_REVIEW_ARTIFACTS.md) · [STEWARD_VERDICT_ATTACH.md](../tutorials/STEWARD_VERDICT_ATTACH.md) |
| **Evidence store / re-decide / packs** | [scan.md](scan.md) (evidence section) | [EVIDENCE_STORE.md](../EVIDENCE_STORE.md) |
| **Contracts & PII export** | [contract.md](contract.md) | [data_scanning.md](../data_scanning.md) |
| **Contract Synthesis** (portable ODCS v3.1) | [synthesize.md](synthesize.md) | version policy note in synthesize.md |
| **LLM enrich** (run / export-context / add-context) | [enrich.md](enrich.md) | [LLM_ENRICHMENT.md](../LLM_ENRICHMENT.md) |
| **Multistep / batch enrich** | [multi-and-batch-enrich.md](multi-and-batch-enrich.md) | [enrich.md](enrich.md) · `scripts/batch_enrich.sh` |
| **LLM list / test / add** | [llm.md](llm.md) | [LLM_PROVIDERS.md](../LLM_PROVIDERS.md) |
| **Catalog → OpenMetadata** | [catalog.md](catalog.md) (`push` / `push-file` / `push-batch` / `push-scan`) | [CATALOG_OPENMETADATA_TUTORIAL.md](../tutorials/CATALOG_OPENMETADATA_TUTORIAL.md) · [PII_CSV_ENRICH_OPENMETADATA_CLI.md](../tutorials/PII_CSV_ENRICH_OPENMETADATA_CLI.md) |
| **Continuous quality monitor** | [monitor.md](monitor.md) (`run` / `batch` / `export` / `airflow generate`) | [QUALITY_SCAN_ALL_WAYS.md](../QUALITY_SCAN_ALL_WAYS.md) §6 · [QUALITY_WORKFLOW_TUTORIAL.md](../tutorials/QUALITY_WORKFLOW_TUTORIAL.md) |
| **Enrich then push (already-created contracts)** | [enrich.md](enrich.md#then-push-to-openmetadata) · [catalog.md](catalog.md) | Air-gap: `enterprise/docs/install/enrich-and-catalog.md` |
| **Config & batch** | [config-batch.md](config-batch.md) | [SCAN_CSV_TO_CONTRACT_GUIDE.md](../SCAN_CSV_TO_CONTRACT_GUIDE.md) |
| **Mask** | [mask.md](mask.md) | [masking_guide.md](../masking_guide.md) |
| **Free-text PII eval** | [pii-eval.md](pii-eval.md) (`pii eval` / `pii eval-build`) | [TEXT_PII_EVAL.md](../TEXT_PII_EVAL.md) · [TEXT_PII_EVAL_TUTORIAL.md](../tutorials/TEXT_PII_EVAL_TUTORIAL.md) |

**Start here:** [tutorials/INTRO.md](../tutorials/INTRO.md) — start/stop the dashboard, scan a CSV, LLM enrich, evidence, multistep enrich, compare contracts, quality deploy, schema-only drift, OpenMetadata.

End-to-end pipeline (CLI + Jupyter): [tutorials/PIPELINE_GUIDE.md](../tutorials/PIPELINE_GUIDE.md).
Scan → enrich → OpenMetadata: [tutorials/SCAN_ENRICH_CATALOG_SGLANG.md](../tutorials/SCAN_ENRICH_CATALOG_SGLANG.md).

**Golden CSV tutorial (PII → enrich → OM):** [tutorials/PII_CSV_ENRICH_OPENMETADATA_CLI.md](../tutorials/PII_CSV_ENRICH_OPENMETADATA_CLI.md).

```bash
redibis --help
redibis <command> --help
redibis <command> <action> --help
```

Use the **same** `--output-dir` (default `./reports`) on every command in a workflow so
scan / show / enrich / contract / catalog share one local store (`<output-dir>/_dev_storage`).
