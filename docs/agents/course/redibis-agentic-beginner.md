# Redibis Agentic Layer — Beginner Course

> **Prerequisites:** Read the [Technology Context](./redibis-agentic-tech-context.md) before starting this track.

## Overview

For engineers who will *work in* the agents package and need to be independently productive:
read and build `PipelineSpec`s, understand every glue function, run the worked example through
the executor, and use the composer/board confidently. 2 days / ~14 hrs / 8 modules. No LangGraph
internals yet (that's Intermediate).

## Schedule

| ID | Module | Day | Duration |
|----|--------|-----|----------|
| M1 | Repo map: core vs agents vs UI | 1 | 1.5 hr |
| M2 | The deterministic core, hands-on | 1 | 2 hr |
| M3 | The node registry & PipelineSpec | 1 | 1.5 hr |
| M4 | Tool runner: every glue function | 1 | 2 hr |
| M5 | Run the worked example as an agent | 2 | 2 hr |
| M6 | Contracts & OpenMetadata publish | 2 | 1.5 hr |
| M7 | The composer & board UI | 2 | 2 hr |
| M8 | Capstone: onboard a CSV end-to-end | 2 | 1.5 hr |

---

### M1 — Repo map: core vs agents vs UI (Day 1, 1.5 hr)

**Learning Objectives**
- Place every package on the dependency map and the one-way import rule.
- Distinguish the deterministic core from the thin agent edge.

**Topics Covered**
- `CLAUDE.md` dependency direction; `scan` ← `profiling`+`quality`+`pii`; `services` glue.
- `redibis/agents/*` as the edge: `registry`, `planner`, `executor`, `tool_runner`, `recipes`, `capability_guard`, `handoff`, `lineage_store`.
- The framework-neutral rule: LangGraph/CopilotKit only in `agents/*` + frontend.
- `webapp/backend.py` `/api/agents/*` routes; `static/agents_composer.js` + `agents.js`.

**Lab**
Draw (on paper or Mermaid) the import arrows from `agents/tool_runner.py` outward. Confirm it imports `scan`, `pii`, `quality`, `contracts`, `store` — and that none of those import `agents`.

---

### M2 — The deterministic core, hands-on (Day 1, 2 hr)

**Learning Objectives**
- Run profile, PII, quality, and masking directly via the facades.
- Inspect the artifacts each produces.

**Topics Covered**
- `from redibis import ProfileScan, PIIScan, QualityScan, RedibisConfig`; `ScanConfig`.
- `ColumnProfile`, `PIIDetection` fields (`column`, `detected`, `entity_type`, `confidence`).
- `MaskingEngine`, `MaskingPlan`, `auto_suggest_plan` from a PII scan.
- `ReportBundle.flush(run_dir)` → `pii_detection_report.html`, `interactive_review.html`.

**Lab**
On a CSV, run `ProfileScan` then `PIIScan`; print the `PIIDetection` list. Build `auto_suggest_plan` from it and print the per-column masking strategy. Flush a `ReportBundle` and open the PII report HTML.

---

### M3 — The node registry & PipelineSpec (Day 1, 1.5 hr)

**Learning Objectives**
- Read the node catalogue and its typed ports.
- Hand-author a valid `PipelineSpec`.

**Topics Covered**
- `register_node` / `list_nodes` / `NodeRegistryEntry`; config schema per node.
- Ports: `contract` requires a DataRef **and** a ProfileResult upstream.
- `PipelineSpec`, `PipelineNode`, `PipelineEdge`; `validate_spec` errors.
- `palette_entries()` (what the composer renders).

**Lab**
Write a `PipelineSpec` in Python for `source → sample → profile → contract(pii=True, quality=True)`. Run `validate_spec` and intentionally break an edge to see the error, then fix it.

---

### M4 — Tool runner: every glue function (Day 1, 2 hr)

**Learning Objectives**
- Trace each node kind to its core call and outputs.
- Understand `state.extras` as the cross-step data bus.

**Topics Covered**
- `_run_source_table` (resolve + load sample), `_ensure_dataframe` (local/external).
- `_run_profile_scan` → `Scan.profile`; `_run_pii_scan` → `detect_pii` + `build_pii_contract` (stashes `pii_partial`).
- `_run_quality_scan` → `run_quality_phase` (writes GE artifacts); `_run_classify` → `run_classification`.
- `_run_contract` (composite: quality+pii+classify+enrich sub_steps); `_persist_run_sample`.
- `_run_contract_write` → `ScanContractWriter` → `RunMerger`; `_run_catalog_push` → `CatalogService.push`.

**Lab**
Add a `print` (or breakpoint) inside three `_run_*` functions, run a spec, and capture what each writes to `state.extras` and returns. Document the data hand-off from PII → contract write.

---

### M5 — Run the worked example as an agent (Day 2, 2 hr)

**Learning Objectives**
- Execute the full intent via `PipelineExecutor` and read the `AgentRun`.
- Use a recipe as a starting point.

**Topics Covered**
- `PipelineExecutor.execute(spec, table)`; sequential vs LangGraph selection.
- `AgentRun.steps[]`, `status`, `session_id`; lineage at `runs/<run_id>/run.json`.
- `recipes.get_recipe(...)` / `recipes_for_intent(...)`.
- `dry_run` semantics (enrich + publish skip).

**Lab**
Pick the `mask_local_csv_pii` recipe, build its spec, run it on a CSV, and print every step's output. Then re-run with `dry_run=True` and note which steps skip and why.

---

### M6 — Contracts & OpenMetadata publish (Day 2, 1.5 hr)

**Learning Objectives**
- Explain how subcontracts merge into one ODCS contract.
- Publish to OpenMetadata safely.

**Topics Covered**
- `ScanContractWriter.write_kind` → subcontract bucket; `RunMerger.merge_run`.
- `ContractStore.upsert()` invariants: identity lock, tag union, PII decision overlay.
- ODCS v3 `schema[]`/`properties[]`; full-schema base (approving one PII column yields a full contract).
- `catalog_push` → `CatalogService.push(table, dry_run=…)`; reading `entity_fqn`.

**Lab**
After M5, fetch the active contract from `ContractStore`, confirm it's full-schema ODCS, then run `CatalogService.push(table, dry_run=True)` and print the would-be OpenMetadata FQN.

---

### M7 — The composer & board UI (Day 2, 2 hr)

**Learning Objectives**
- Drive Ask, Composer, Dashboard, and Results as an operator would.
- Hand off a table into the manual Scan console.

**Pre-read (required):** [`docs/agents/howto/`](../howto/README.md) modules
[00](../howto/00-getting-started.md)–[03](../howto/03-dashboard-and-results.md).

**Topics Covered**
- Typed URL `/agents` (no main-nav discovery); four primary tabs.
- Ask runtime panel; Composer palette / validate / preview / run.
- HITL resume when `auto_approve_writes` is false; handoff identifiers
  ([`SESSIONS_AND_HANDOFF.md`](../SESSIONS_AND_HANDOFF.md)).

**Lab**
Complete howto Recipe B (Composer + gate) and Recipe A handoff from
[`08-recipes.md`](../howto/08-recipes.md).

---

### M8 — Capstone: onboard a CSV end-to-end (Day 2, 1.5 hr)

**Learning Objectives**
- Independently take a CSV from raw to a published, governed contract.

**Topics Covered**
- Full chain: source → sample → profile → pii → mask (suggest) → classify → contract → gate → publish.
- Verifying artifacts, contract version, and OM dry-run FQN.

**Lab**
Build the full spec (or compose it in the UI) for a new CSV, run it with an approval gate, resume after approval, publish with `dry_run=True`, and hand off to the manual console to confirm the same contract is visible there.

---

## Quick Reference

| Command / Pattern | What It Does |
|---|---|
| `ProfileScan(ScanConfig(table=…)).run(df)` | Profile a DataFrame |
| `PIIScan(...).run(df)` | PII scan (evidence + verdicts) |
| `QualityScan(...).run(df)` | Quality rules |
| `auto_suggest_plan(scan_result)` | Suggest masking per PII column |
| `MaskingEngine(plan).apply(df, keys)` | Apply a masking plan |
| `ReportBundle(run, cfg).flush(run_dir)` | Write report artifacts |
| `register_node(entry)` / `list_nodes()` | Registry read/write |
| `validate_spec(spec)` | Validate a pipeline |
| `PipelineNode(kind=…, params=…)` | Build a node |
| `recipes_for_intent(text)` | Few-shot/start-from recipes |
| `PipelineExecutor(...).execute(spec, table)` | Run a spec |
| `AgentRun.steps[].output` | Per-step results |
| `ScanContractWriter.write_kind(...)` | Write a subcontract |
| `RunMerger.merge_run(kind, table, run_id)` | Merge into active |
| `ContractStore.upsert(...)` | Only contract writer |
| `CatalogService.push(table, dry_run=…)` | Publish to OpenMetadata |
| `POST /api/agents/batch` | Start a batch run |
| `GET /api/agents/runs/{id}` | Run status + steps |

## Common Mistakes

| Mistake | How to Avoid It |
|---|---|
| Importing `agents` from a core package | Keep imports one-way; core never imports agents |
| Building a spec with `contract` but no profile upstream | Feed both DataRef and ProfileResult into `contract` |
| Reading PII detail from `state.extras` after the run | It's in-memory; durable detail is in step outputs / artifacts |
| Calling `ContractStore` write methods directly | Use the writer→merger→upsert chain |
| Forgetting `dry_run` on first OM push | Always dry-run first; inspect the FQN |
| Expecting reports without a persisted sample | `_persist_run_sample` writes `sample.csv`; check it exists |

## What to Learn Next

1. **Intermediate track** — LangGraph executor, HITL interrupt/resume, registry/planner internals.
2. Study `agents/recipes.py` and `docs/agents/REDIBIS_CAPABILITY_MAP.md`.
3. Read `docs/agents/SESSIONS_AND_HANDOFF.md` for the agent↔manual boundary.
