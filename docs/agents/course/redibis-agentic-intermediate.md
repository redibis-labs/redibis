# Redibis Agentic Layer — Intermediate Course

> **Prerequisites:** Read the [Technology Context](./redibis-agentic-tech-context.md) before starting this track. Assumes the Beginner track is complete.

## Overview

For engineers extending the agents package. You'll learn the LangGraph executor internals,
human-in-the-loop via `interrupt()`/resume, how the planner turns NL into a validated spec, the
capability guard, batch fan-out, and the agent↔manual handoff — production-ready patterns, not
basics. 3 days / ~21 hrs / 11 modules.

## Schedule

| ID | Module | Day | Theme | Duration |
|----|--------|-----|-------|----------|
| I1 | LangGraph executor anatomy | 1 | Production config & orchestration | 2 hr |
| I2 | Compiling a spec → graph | 1 | | 1.5 hr |
| I3 | Runtime context & the tool node | 1 | | 1.5 hr |
| I4 | HITL: interrupt, AWAITING_HITL, resume | 2 | Testing, debugging & control | 2 hr |
| I5 | Cancellation & durability | 2 | | 1.5 hr |
| I6 | Lineage, SSE logs & OTel audit | 2 | | 1.5 hr |
| I7 | IntentPlanner: NL → PipelineSpec | 2 | | 1.5 hr |
| I8 | Recipes as few-shot grounding | 3 | Advanced patterns & capstone | 1.5 hr |
| I9 | Capability guard & codegen seam | 3 | | 1.5 hr |
| I10 | Batch fan-out & shared checkpointer | 3 | | 2 hr |
| I11 | Capstone: a new node end-to-end | 3 | | 2.5 hr |

---

## Day 1 — Production Configuration & Orchestration

### I1 — LangGraph executor anatomy (2 hr)
**Objectives:** read `executor.py` top to bottom; explain `execute()` return `(AgentRun, AgenticSession)`.
**Topics:** `LangGraphExecutor.execute(spec, table, …)`; `compile_prompt_plan`; `AgenticSession.from_spec`; `save_manifest`; `runtime` dict passed to `compile()` (concurrency-safe, not on `self`); `_finalize_run`; status lifecycle `PENDING→RUNNING→COMPLETED/FAILED/CANCELLED/AWAITING_HITL`.
**Lab:** instrument `execute()` to print the `runtime` dict and the final `_GraphState`; run a 4-node spec and capture the state transitions.

### I2 — Compiling a spec → graph (1.5 hr)
**Objectives:** understand how nodes/edges become a LangGraph `StateGraph`.
**Topics:** `compile(spec, runtime=…, validate=…)`; `_GraphState` (`table`, `node_index`, `steps`, `status`, `error`); node functions invoking `tool_runner`; `thread_id`/`configurable` for the checkpointer.
**Lab:** add a logging wrapper around the per-node callable to print `node_kind` + elapsed ms for each step of a run.

### I3 — Runtime context & the tool node (1.5 hr)
**Objectives:** see how `ToolContext` flows into every node.
**Topics:** `_build_tool_context`; `ToolContext` fields (`contract_store`, `sub_store`, `run_merger`, `sample_dir`, `source_config`, `spark`, `dry_run`, `execute`); `_source_override_from_batch` (composer connection → live source).
**Lab:** run the same spec twice — once `dry_run=True`, once `False` — and diff which steps skip and which write artifacts.

---

## Day 2 — Testing, Debugging & Control

### I4 — HITL: interrupt, AWAITING_HITL, resume (2 hr)
**Objectives:** implement a gated run and resume it.
**Topics:** `gate` node → `interrupt()`; run becomes `AWAITING_HITL`; `hitl_pending`/`batch_meta.paused_table`; `executor.resume(run_id, …)` with `Command(resume=…)`; `/api/agents/runs/{id}/resume`.
**Lab:** build `… → gate → contract_write → publish`, run until `AWAITING_HITL`, inspect `run.hitl_pending`, then resume with approval and confirm the contract writes.

### I5 — Cancellation & durability (1.5 hr)
**Objectives:** cancel a run cleanly and reason about resumability.
**Topics:** `LineageStore.cancellation_token` / `request_cancel`; cooperative cancellation in node loop; checkpointer-backed resume; `drop_token`.
**Lab:** start a multi-table run, `POST /api/agents/runs/{id}/cancel`, and verify partial/cancelled table statuses via `table_status_rows`.

### I6 — Lineage, SSE logs & OTel audit (1.5 hr)
**Objectives:** observe a run live and after the fact.
**Topics:** `LineageStore.save/load`; `run.json` shape; `GET /api/agents/runs/{id}/stream` (SSE); `dag_trace.build_audit_dag`; OTel spans per step.
**Lab:** open the composer Debug drawer (or `curl` the SSE endpoint) during a run; then fetch `/api/agents/runs/{id}/audit` and map spans to steps.

### I7 — IntentPlanner: NL → PipelineSpec (1.5 hr)
**Objectives:** turn a sentence into a validated plan.
**Topics:** `planner.py` `IntentPlanner`; `PlannerContext` (catalogue + `memory_recipes`); `heuristic_plan` vs LLM plan (`agents.planner_provider`); validation loop against `validate_spec`.
**Lab:** call the planner with "classify the Oracle schema and build contracts, don't publish" and assert the emitted spec validates and contains `classify` but no `publish`.

---

## Day 3 — Advanced Patterns & Capstone

### I8 — Recipes as few-shot grounding (1.5 hr)
**Objectives:** add a golden recipe that stays valid.
**Topics:** `recipes.RECIPES` schema; `recipes_for_intent` ranking; `_assert_recipes_valid`; index-based node/edge JSON matching planner output.
**Lab:** add a new recipe (e.g. "profile + quality only, no PII"), then run `pytest tests/test_agent_recipes.py` to prove it validates.

### I9 — Capability guard & codegen seam (1.5 hr)
**Objectives:** keep the agent on the native path.
**Topics:** `capability_guard.decide_codegen`; `CAPABILITY_KEYWORDS`; `/api/agents/codegen/submit` returning `native_capability_available`; `force_external`; egress validation + `hard_block`.
**Lab:** submit "mask the email column" to codegen and assert it returns `native_capability_available`; submit "call an external REST API" and assert it proceeds.

### I10 — Batch fan-out & shared checkpointer (2 hr)
**Objectives:** run many tables with one shared durable state.
**Topics:** `BatchExecutor` per-table LangGraph; `resolve_tables`; `resume_batch`/`_continue_langgraph_batch`; Postgres checkpointer when `memory.enabled` + `memory.dsn_ref`; `/api/agents/batch`.
**Lab:** run a 3-table batch from `/api/agents/batch`, pause one table at a gate, resume the batch, and verify per-table statuses.

### I11 — Capstone: a new node end-to-end (2.5 hr)
**Objectives:** add a governed capability and expose it.
**Topics:** register a node + ports + config schema; bind it in `tool_runner` to a core call; add to `CAPABILITY_KEYWORDS`; surface in the composer palette.
**Lab:** add a `retention` node that annotates a retention policy on the contract via the core helper, wire its `_run_*`, validate a spec using it, and run it through the executor.

---

## Quick Reference

| Command / Pattern | What It Does |
|---|---|
| `LangGraphExecutor(...).execute(spec, table)` | Run a spec on LangGraph |
| `executor.resume(run_id, …)` | Resume an `AWAITING_HITL` run |
| `compile(spec, runtime=…, validate=…)` | Spec → LangGraph app |
| `interrupt(value)` | Pause for human approval |
| `Command(resume=…)` | Continue a paused graph |
| `LineageStore.request_cancel(run_id)` | Cooperative cancel |
| `LineageStore.load(run_id)` | Load an `AgentRun` |
| `build_audit_dag(run)` | Audit DAG trace |
| `GET /api/agents/runs/{id}/stream` | SSE log stream |
| `IntentPlanner(...).plan(intent, ctx)` | NL → `PipelineSpec` |
| `PlannerContext(memory_recipes=…)` | Few-shot grounding |
| `recipes_for_intent(text, k)` | Rank recipes |
| `_assert_recipes_valid()` | Recipe validity guard |
| `decide_codegen(intent, force_external)` | Native-vs-codegen policy |
| `native_capability_surface()` | Live capability list |
| `BatchExecutor(...).run(spec, tables=…)` | Batch fan-out |
| `resolve_tables(spec, tables=…)` | Resolve target tables |
| `POST /api/agents/batch` | Start a batch |
| `POST /api/agents/runs/{id}/resume` | Resume via API |
| `table_status_rows(run)` | Per-table status |

## Common Mistakes

| Mistake | How to Avoid It |
|---|---|
| Mutating `self` with per-run state | Pass run state via the `runtime` dict to `compile()` (concurrency) |
| Calling `interrupt()` outside a gate node | Keep HITL at gate nodes; resume via `Command` |
| Cancelling without a token | Use `LineageStore.cancellation_token`/`request_cancel` |
| Hand-editing the planner prompt with a static catalogue | Build context live from registry + recipes |
| Adding a node without ports/validation | Define input/output ports so `validate_spec` enforces wiring |
| Batch resume losing state | Use the shared Postgres checkpointer (`memory.dsn_ref`) |

## What to Learn Next

1. **Mastery track** — CopilotKit/AG-UI chat, memory loop, codegen service, OM at scale, capstone.
2. Read `agents/executor.py` + `agents/batch_executor.py` alongside the LangGraph HITL docs.
3. Study `docs/agents/AGENTIC_GOVERNANCE_DESIGN.md`.

## Code Patterns Appendix

**1. Build and validate a spec**
```python
from redibis.agents.models import PipelineSpec, PipelineNode, PipelineEdge
from redibis.agents.registry import validate_spec

nodes = [
    PipelineNode(kind="source", label="Source", params={"engine": "folder"}),
    PipelineNode(kind="sample", label="Sample", params={"strategy": "all"}),
    PipelineNode(kind="profile", label="Profile", params={"engine": "great_expectations"}),
    PipelineNode(kind="contract", label="Contract", params={"pii": True, "quality": True}),
]
edges = [
    PipelineEdge(source=nodes[0].id, target=nodes[1].id),
    PipelineEdge(source=nodes[1].id, target=nodes[2].id),
    PipelineEdge(source=nodes[1].id, target=nodes[3].id),  # DataRef into contract
    PipelineEdge(source=nodes[2].id, target=nodes[3].id),  # ProfileResult into contract
]
spec = PipelineSpec(name="onboard", goal="profile + contract", nodes=nodes, edges=edges)
assert not validate_spec(spec)
```

**2. Run with HITL and resume**
```python
from redibis.agents.executor import LangGraphExecutor
run, session = LangGraphExecutor(lineage, tool_ctx=ctx, redibis_config=cfg).execute(spec, "telecom.customers")
if run.status.value == "awaiting_hitl":
    run = LangGraphExecutor(lineage, tool_ctx=ctx).resume(run.run_id, table="telecom.customers", approved=True)
```

**3. Plan from natural language**
```python
from redibis.agents.planner import IntentPlanner, PlannerContext
from redibis.agents.registry import catalog_for_planner
from redibis.agents.recipes import recipes_for_intent

ctx = PlannerContext(catalog=catalog_for_planner(),
                     memory_recipes=recipes_for_intent("classify the oracle schema, no publish"))
spec = IntentPlanner(cfg).plan("classify the oracle schema, no publish", ctx)
```

**4. Codegen guard**
```python
from redibis.agents.capability_guard import decide_codegen
d = decide_codegen("mask the email column")
assert d.generate is False and any(m.id == "mask" for m in d.native_matches)
```
