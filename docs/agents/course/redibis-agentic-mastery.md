# Redibis Agentic Layer — Mastery Programme

> **Prerequisites:** Read the [Technology Context](./redibis-agentic-tech-context.md) before starting this track. Assumes Beginner + Intermediate complete.

## Overview

The complete picture: framework internals (LangGraph + CopilotKit/AG-UI), the memory loop,
durable batch at scale, the codegen service seam, OpenMetadata publishing, observability/RAI, and
a capstone that onboards a full schema unattended with human gates. 5 days / ~35 hrs / 5
day-blocks. Every module is lab-anchored on the running example (CSV → scan → profile → mask →
LLM → contract → OM), scaled to production.

## Schedule

| Day | Theme |
|-----|-------|
| 1 | Orchestration internals (LangGraph) |
| 2 | Extensibility: nodes, profilers, recipes, capability surface |
| 3 | Scale: batch, checkpointer, memory loop, parallelism |
| 4 | Agentic UI: CopilotKit / AG-UI + the composer |
| 5 | Production: OM, codegen service, RAI/OTel, security, capstone |

---

## Day 1 — Orchestration internals (LangGraph)

**1.1 — Graph compilation & state (2 hr)**
Objectives: master `compile()`, `_GraphState`, node functions, thread/checkpointer config.
Topics: `executor.compile(spec, runtime=…)`; per-node callable invoking `tool_runner.run_node`; `node_index` advancement; error capture into `_GraphState`.
Lab: re-implement a 3-node graph by hand with `StateGraph` and confirm it produces the same `AgentRun.steps` as the executor.

**1.2 — HITL deep dive (2 hr)**
Objectives: model multi-gate flows and partial approvals.
Topics: `interrupt()` payloads; `hitl_pending.interrupts`; `batch_meta.paused_table`; `resume` with `Command(resume=…)`; idempotent resume.
Lab: build a flow with two gates (pre-contract, pre-publish); resume each independently; verify the run only writes after both.

**1.3 — Durability & replay (2 hr)**
Objectives: reason about crash recovery and audit replay.
Topics: checkpointer semantics; `LineageStore` persistence cadence; `dag_trace`; replaying a run from `run.json`.
Lab: kill the process mid-run (sequential vs LangGraph), restart, and resume; compare recovered state.

---

## Day 2 — Extensibility

**2.1 — Add a node end-to-end (2 hr)**
Objectives: register + bind + validate + expose a new capability.
Topics: `register_node`, ports, `config_schema`; bind in `tool_runner`; composer palette via `palette_entries()`.
Lab: add a `retention` node (annotate retention policy), wire `_run_retention`, validate, run.

**2.2 — Profiler engines & Tier-2 (2 hr)**
Objectives: swap profiling engines and add a deep capability.
Topics: `profiling` registry (`GE`/`OpenMetadata`/`YData`); `deep_profile.list_capabilities` (Tier-2, config-bounded); `/api/agents/capabilities`.
Lab: run the profile node with `engine="ydata"`, then enable a Tier-2 capability and confirm it appears in `native_capability_surface()`.

**2.3 — Recipes & planner grounding (1.5 hr)**
Objectives: curate few-shot exemplars that don't drift.
Topics: `RECIPES`, `recipes_for_intent`, `_assert_recipes_valid`; planner context assembly.
Lab: add two recipes, run `pytest tests/test_agent_recipes.py`, and confirm the planner picks the right one for a matching intent.

**2.4 — Capability map maintenance (1.5 hr)**
Objectives: keep "external code only if not native" honest.
Topics: `CAPABILITY_KEYWORDS`; `decide_codegen`; `docs/agents/REDIBIS_CAPABILITY_MAP.md`; `tests/test_capability_guard.py`.
Lab: add a capability + phrases, extend the guard test, and verify a matching codegen intent is blocked.

---

## Day 3 — Scale

**3.1 — Batch fan-out (2 hr)**
Objectives: run hundreds of tables governed.
Topics: `BatchExecutor`, `resolve_tables`, per-table LangGraph, `/api/agents/batch` (background task).
Lab: batch over a schema's tables; observe `table_status_rows` and the dashboard.

**3.2 — Shared checkpointer & resume_batch (2 hr)**
Objectives: durable batch with mid-flight pauses.
Topics: Postgres checkpointer (`memory.enabled` + `memory.dsn_ref`); `resume_batch` / `_continue_langgraph_batch`.
Lab: pause one table at a gate, restart the service, `resume_batch`, confirm continuity.

**3.3 — The memory loop (2 hr)**
Objectives: feed past human decisions back as suggest-only context.
Topics: `memory/*` (fingerprint, pgvector store, retriever, consent, rationale); format-signature-only persistence; `PlannerContext.memory_recipes` and classify `use_memory`.
Lab: enable memory, classify a table, and show a later run surfaces a prior-decision hint in the review queue.

---

## Day 4 — Agentic UI (CopilotKit / AG-UI)

**4.1 — The bridge (2 hr)**
Objectives: attach a chat client to the governed graph.
Topics: `copilotkit_bridge.py`; `/api/copilotkit/agent` when `agents.copilotkit_enabled`; `ag-ui-langgraph`; `build_governance_graph`; `is_planning_intent`.
Lab: enable `copilotkit_enabled`, send a planning message, and confirm it returns a plan reply grounded in `catalog_for_planner()`.

**4.2 — Composer as a thin client (2 hr)**
Objectives: map every composer control to a backend endpoint.
Topics: `agents_composer.js` state; Config→Setup (provider/keys/packs/recipes); Send→`/api/agents/batch`; Debug→`/api/agents/runs/{id}/stream`; Reports→summary; Open-in-console→handoff.
Lab: trace a Send click through the network tab to `/api/agents/batch`, then to the SSE stream and the summary endpoint.

**4.3 — Generative UI & shared state (1.5 hr)**
Objectives: understand AG-UI shared agent↔UI state and HITL in chat.
Topics: AG-UI protocol; surfacing `interrupt()` as a chat approval; keeping domain logic server-side.
Lab: drive a gated run from the chat panel and approve it from chat; confirm the same `resume` path runs.

---

## Day 5 — Production

**5.1 — OpenMetadata publishing (2 hr)**
Objectives: publish governed contracts to OM reliably.
Topics: `catalog_push` → `CatalogService.push`; `dry_run`; entity FQN; idempotent re-publish; Atlas (planned).
Lab: publish a contract to a real OM dev instance after a gate; re-publish and confirm idempotency.

**5.2 — Codegen service seam (2 hr)**
Objectives: understand proposed-not-executed codegen + the commercial GCP service.
Topics: `codegen.py` safety chain (`propose`/`scan`/`judge`/`await_human`); `submit_codegen`; `codegen_service_url` (GCP client); egress audit; dynamic tool registry (`/api/agents/dynamic-tools/register`).
Lab: submit a non-native codegen intent, inspect the egress-validated request + vuln/judge verdicts, and register the (sandboxed) tool.

**5.3 — RAI, OTel & security (2 hr)**
Objectives: govern every model call and audit everything.
Topics: `telemetry/model_gateway.guarded_model_call` (RAI + OTel) on every model call incl. the planner; `pii_scope` structural PII; `hard_block_external_pii`; credential references (never raw secrets).
Lab: route an enrich call through `guarded_model_call`, confirm an OTel span + RAI check, and verify a raw-PII egress is blocked.

**5.4 — Capstone: unattended schema onboarding (3 hr)**
Objectives: ship the whole thing.
Topics: NL intent → planner → batch over a schema → gates → contracts → OM, with memory + audit + handoff.
Lab: from one intent, onboard an entire schema: plan, run the batch with a steward gate per table, publish to OM (dry-run then real on one table), open the audit DAG, and hand off one flagged table to the manual console for deep review.

---

## Quick Reference

| Command / Pattern | What It Does |
|---|---|
| `executor.compile(spec, runtime=…)` | Spec → LangGraph app |
| `executor.execute(spec, table)` | Run; returns `(AgentRun, AgenticSession)` |
| `executor.resume(run_id, …)` | Resume `AWAITING_HITL` |
| `interrupt(value)` / `Command(resume=…)` | HITL pause/continue |
| `BatchExecutor(...).run(spec, tables=…)` | Batch fan-out |
| `resume_batch(run_id, …)` | Resume a batch |
| `LineageStore.save/load/request_cancel` | Durable run records + cancel |
| `build_audit_dag(run)` | Audit DAG |
| `GET /api/agents/runs/{id}/stream` | SSE logs |
| `IntentPlanner(cfg).plan(intent, ctx)` | NL → spec |
| `PlannerContext(memory_recipes=…)` | Few-shot grounding |
| `recipes_for_intent` / `_assert_recipes_valid` | Recipe ranking + guard |
| `register_node` / `palette_entries` | Add a node + UI |
| `get_profiler` / `PROFILER_REGISTRY` | Profiler engines |
| `deep_profile.list_capabilities` | Tier-2 capabilities |
| `decide_codegen(intent, force_external)` | Native-vs-codegen guard |
| `submit_codegen(...)` | Egress-safe codegen submit |
| `CatalogService.push(table, dry_run=…)` | Publish to OpenMetadata |
| `guarded_model_call(...)` | RAI + OTel wrapper for every model call |
| `copilotkit_bridge` / `/api/copilotkit/agent` | AG-UI chat endpoint |
| `handoff_to_manual_session(run, table, …)` | Agent → manual session |
| `ContractStore.upsert(...)` | The only contract writer |

## Common Mistakes

| Mistake | How to Avoid It |
|---|---|
| Putting domain logic in the CopilotKit bridge | Keep it server-side; the bridge only plans/chats |
| Model calls bypassing the gateway | Route *every* call (incl. planner) through `guarded_model_call` |
| Executing generated code | It's proposed-not-executed: scan + judge + human + sandbox |
| Persisting raw samples/secrets in memory store | Format-signature-only; credential references, never raw secrets |
| Publishing to OM without idempotency thought | Dry-run first; rely on identity-locked contracts |
| Registering agent sessions in `session_manager` | Agent uses `AgenticSession` in lineage; only handoff mints a manual session |
| Letting batch lose state on restart | Use the shared Postgres checkpointer |

## Code Patterns Appendix

**1. End-to-end intent → batch → publish (programmatic)**
```python
from redibis.agents.planner import IntentPlanner, PlannerContext
from redibis.agents.registry import catalog_for_planner
from redibis.agents.recipes import recipes_for_intent
from redibis.agents.batch_executor import BatchExecutor

intent = "Onboard all tables in sales: profile, detect PII, classify, contract, publish to OpenMetadata"
ctx = PlannerContext(catalog=catalog_for_planner(), memory_recipes=recipes_for_intent(intent))
spec = IntentPlanner(cfg).plan(intent, ctx)
run = BatchExecutor(lineage, tool_ctx=tool_ctx).run(spec, tables=resolve_tables(spec, database="sales"))
```

**2. Resume a gated batch**
```python
from redibis.agents.batch_executor import BatchExecutor
bx = BatchExecutor(lineage, tool_ctx=tool_ctx)
run = bx.resume_batch(run_id, table="sales.customers", approved=True, approved_by="steward")
```

**3. Govern a model call**
```python
from redibis.telemetry.model_gateway import guarded_model_call
result = guarded_model_call(lambda: provider.complete(prompt), purpose="enrich", table=table)
```

**4. Publish to OpenMetadata after approval**
```python
res = ctx.catalog_service.push("sales.customers", dry_run=False)  # gate must be satisfied upstream
print(res.backend, res.entity_fqn)
```

**5. Handoff one table to the manual console**
```python
from redibis.agents.handoff import handoff_to_manual_session
out = handoff_to_manual_session(run, "sales.customers",
                                scan_output_dir=scan_dir, runs_output_dir=runs_dir,
                                session_manager=session_manager)
print(out["redirect_url"])  # /?session=<id>
```
