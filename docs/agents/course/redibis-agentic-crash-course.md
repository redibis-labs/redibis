# Redibis Agentic Layer — One-Day Crash Course

> **Prerequisites:** Read the [Technology Context](./redibis-agentic-tech-context.md) before starting this track.

## Overview

For engineers who already know the redibis domain (scan/PII/contracts) and need to understand the
**agentic layer** fast. By the end you can read a `PipelineSpec`, trace how a node becomes a real
redibis call, run the worked example end-to-end (CSV → scan → profile → mask → LLM → contract →
OpenMetadata), and drive it from the composer. 1 day / ~8 hrs / 5 sessions, lab-first.

## Schedule

| ID | Session | Duration |
|----|---------|----------|
| S1 | The glue: nodes → deterministic calls | 1.5 hr |
| S2 | Run the worked example by hand (core) | 2 hr |
| S3 | Same flow as an agent: PipelineSpec + LangGraph | 1.5 hr |
| S4 | Human-in-the-loop, publish to OM, handoff | 1.5 hr |
| S5 | Drive it from the composer + Debug SSE | 1.5 hr |

---

### S1 — The glue: nodes → deterministic calls (1.5 hr)

**Learning Objectives**
- Explain how a node `kind` maps to one deterministic redibis call.
- Locate the binding for any node in `tool_runner.py`.
- Read the node catalogue from the registry.

**Topics Covered**
- The 8 node kinds in `registry.py`: `source`, `sample`, `profile`, `contract`, `mask`, `publish`, `gate`, `batch`.
- `tool_runner._run_profile_scan` / `_run_pii_scan` / `_run_quality_scan` / `_run_contract` / `_run_classify` / `_run_enrich` / `_run_contract_write` / `_run_catalog_push`.
- `TableRunState` + `state.extras` as the per-table scratchpad (`pii_partial`, `quality_contract`, `classification`).
- Invariant: detector = evidence, equation = verdict; `ContractStore.upsert()` is the only writer.
- `catalog_for_planner()` — the registry serialized for planning.

**Lab**
Run `python -c "from redibis.agents.registry import catalog_for_planner; import json; print(json.dumps(catalog_for_planner(), indent=2))"` and map each node `type` to its `_run_*` function in `tool_runner.py`. Write a one-line note per node: "node X → calls Y in the core."

---

### S2 — Run the worked example by hand (2 hr)

**Learning Objectives**
- Execute the full governance flow using only the deterministic core (no agent).
- Produce an ODCS contract from a CSV and inspect it.

**Topics Covered**
- `from redibis import PIIScan, ProfileScan, QualityScan, RedibisConfig` and `ScanConfig`.
- Loading a CSV into a DataFrame; `ProfileScan(...).run(df)` then `PIIScan(...).run(df)`.
- `MaskingEngine` + `auto_suggest_plan` for a masking recommendation.
- `ScanContractWriter.write_kind("pii"/"quality", …)` → `RunMerger.merge_run(...)` → `ContractStore.upsert()`.
- Where artifacts land: `report.output_dir/<run_id>/` (`sample.csv`, `pii_contract.yaml`, `interactive_review.html`).

**Lab**
Take any CSV (e.g. `telecom.customers`). In a Python REPL: profile it, run a PII scan, print detected columns, build a masking plan suggestion, then write + merge a PII contract and `print` the active contract from `ContractStore`. Verify a contract YAML exists under the run folder.

---

### S3 — Same flow as an agent: PipelineSpec + LangGraph (1.5 hr)

**Learning Objectives**
- Express the S2 flow as a validated `PipelineSpec`.
- Run it through the executor and read the resulting `AgentRun`.

**Topics Covered**
- Building a spec: nodes `source → sample → profile → contract(pii,quality,classify)` with edges; `registry.validate_spec`.
- `PipelineExecutor.execute(spec, table)` choosing LangGraph vs sequential.
- `AgentRun` shape: `run_id`, `status`, `steps[]` (`node_kind`, `status`, `output`, `error`), `session_id`.
- Why `contract` needs BOTH a DataRef and a ProfileResult upstream (two edges in).
- Recipes: `recipes_for_intent("mask the PII in these CSVs")`.

**Lab**
Build the spec in Python (or copy a recipe from `agents/recipes.py`), run `PipelineExecutor(...).execute(spec, "telecom.customers")`, and print each `StepRecord`'s `node_kind`/`status`/`output`. Confirm it produced the same contract as S2.

---

### S4 — Human-in-the-loop, publish to OM, handoff (1.5 hr)

**Learning Objectives**
- Add an approval gate before publish and resume after approval.
- Publish a contract to OpenMetadata and hand a table off to the manual console.

**Topics Covered**
- `gate` node + `interrupt()`; run status `AWAITING_HITL`; `executor.resume(...)` with `Command(resume=…)`.
- `_run_catalog_push` → `CatalogService.push(table, dry_run=…)` → OpenMetadata FQN.
- `dry_run=True` first; reading `entity_fqn`/`backend` from the result.
- Handoff: `POST /api/agents/handoff {run_id, table}` → `redirect_url=/?session=<id>`.
- Why handoff needs `sample.csv` persisted (the crash fix) — `_persist_run_sample`.

**Lab**
Extend the S3 spec with `… → gate → publish`. Run it, observe `AWAITING_HITL`, resume with approval, and call `catalog_push` with `dry_run=True`. Then `POST /api/agents/handoff` for the table and open the returned `redirect_url` in the manual console.

---

### S5 — Drive it from the composer + Debug SSE (1.5 hr)

**Learning Objectives**
- Produce and run the same pipeline from the `/agents` composer with zero code.
- Watch the live log and open the generated reports.

**Topics Covered**
- Composer Config → **Setup**: model provider + keys, policy/classification packs, recipe picker.
- Toggling step icons (source/profile/pii/mask/contract/publish) → the generated instruction.
- **Send** (gated on a validated provider) → batch run; the live log bar.
- **Debug** icon → SSE log stream (`/api/agents/runs/{id}/stream`).
- **Reports** (inline summary) + **Open in console** (handoff); **Clear jobs** on the dashboard.

**Lab**
On `/agents`: configure a provider in Setup, pick the "Mask PII in local CSVs" recipe, upload a CSV, Send, open the Debug drawer to watch the log, then open Reports and "Open in console" for one table.

---

## Quick Reference

| Command / Pattern | What It Does |
|---|---|
| `catalog_for_planner()` | Serialize the node catalogue for the planner |
| `validate_spec(spec)` | Validate a `PipelineSpec` against the registry |
| `recipes_for_intent(text, k=3)` | Top-k golden plans for an intent |
| `PIIScan(ScanConfig(table=…)).run(df)` | Deterministic PII scan |
| `ProfileScan(...).run(df)` | Deterministic profile |
| `MaskingEngine` + `auto_suggest_plan` | Masking recommendation |
| `ScanContractWriter.write_kind("pii", …)` | Write a PII subcontract |
| `RunMerger.merge_run("pii", table, run_id)` | Merge a subcontract into active |
| `ContractStore.upsert(...)` | The only writer to the contracts bucket |
| `PipelineExecutor(...).execute(spec, table)` | Run a spec (LangGraph/sequential) |
| `executor.resume(run_id, …)` | Resume an `AWAITING_HITL` run |
| `CatalogService.push(table, dry_run=…)` | Publish a contract to OpenMetadata |
| `POST /api/agents/handoff` | Hand a table to the manual console |
| `GET /api/agents/runs/{id}/stream` | Live SSE log for a run |

## Common Mistakes

| Mistake | How to Avoid It |
|---|---|
| Expecting the LLM to detect PII | It only plans/enriches; the detector+equation decide |
| Upserting a contract from your own code | Go through `ScanContractWriter` → `RunMerger` → `ContractStore.upsert()` |
| Forgetting the `gate` before `publish` | Keep `gate` upstream; the executor enforces approval |
| Running with no resolvable sample | Set `sample_dir`/sample_paths or external creds; check the step `reason` |
| Publishing for real in a demo | Use `dry_run=True` on `catalog_push` first |
| Reusing the agent run id as a manual session blindly | Handoff materialises the session; don't hand-mint |

## What to Learn Next

1. **Beginner track** — the deterministic glue in depth and your first multi-step pipeline.
2. **Intermediate track** — LangGraph executor internals, HITL interrupt/resume, registry/planner.
3. Read `docs/agents/REDIBIS_CAPABILITY_MAP.md` and `docs/agents/SESSIONS_AND_HANDOFF.md`.
