# Redibis Agentic Layer — Technology Context

> Shared foundation for all four training tracks. Read this before any track.
> The running example used throughout the course: **grab a CSV → scan → profile → mask →
> LLM enrich → build an ODCS contract → push it to OpenMetadata** — first by hand (the
> deterministic core), then orchestrated by an agent (LangGraph), then driven from a chat UI
> (CopilotKit / AG-UI).

---

## 1. Definition

The **redibis agentic layer** is a thin orchestration tier that turns natural-language intent
("onboard this schema, classify it, build contracts, publish to OpenMetadata") into a
**validated pipeline of redibis's own deterministic functions**, executes it durably, and pauses
for human approval at judgment points. It is a **workflow orchestrator** (category: agentic
graph executor + governed tool surface), not an "AI that does data governance" — every
data-touching action is a call into the same battle-tested redibis core that the manual web
console uses. It complements, rather than replaces, the manual 360° console: both write through
the *same* `ContractStore.upsert()` and produce the *same* ODCS v3 contracts. The distinctive
choice is **framework-neutral core + thin edge adapters**: LangGraph and CopilotKit live only in
`redibis/agents/*` and a frontend, never in the scan/contract engine. This content targets the
open-core agents package on **LangGraph** (durable checkpointer + `interrupt()`), **CopilotKit /
AG-UI** for chat, and **LiteLLM** for model calls.

---

## 2. Problem It Solves

**Before:** onboarding a table to a governed catalog was a manual, click-by-click slog. An
engineer opened the console, uploaded a sample, ran a profile, ran a PII scan, eyeballed the
detections, picked masking rules, hand-edited a contract, and pushed to the catalog — one table
at a time. Without the agentic layer, teams face: (1) no way to run the same governed workflow
across *hundreds* of tables unattended; (2) every "automation" attempt becoming a pile of
one-off scripts that bypass the contract store's invariants (identity locking, tag-union merge,
PII decision overlay); (3) LLM "assistants" that hallucinate verdicts because they aren't bound
to the real detection/equation engine.

**After:** a single intent fans out to a **batch** of per-table runs, each executing the exact
same deterministic steps as the manual flow, with the LLM constrained to *propose* (plans,
business definitions) and *never* to fabricate verdicts or write contracts directly. Runs are
**durable** (resumable after a crash), **gated** (a steward approves before anything irreversible
or external happens), and **auditable** (every step is a lineage record). A run can be **handed
off** to the manual console for deep review on any single table — same session, same artifacts.

---

## 3. Why It Matters / Why Learn It Now

Data-contract and catalog mandates (ODCS adoption, OpenMetadata/Atlas rollouts, privacy
regulation) are pushing teams from "scan one table in a UI" to "continuously govern the whole
estate." That is an *orchestration* problem, and the industry has converged on durable graph
executors (LangGraph) plus agent-native UIs (CopilotKit/AG-UI) to solve it without hand-rolling
state machines. Learning this layer unlocks a class of problems that were previously intractable
in redibis: unattended multi-table onboarding, human-in-the-loop approval at scale, and a chat
surface over a governed tool set. Concrete signal of relevance: the agents package ships Phases
0–8 complete (node registry → planner → LangGraph executor → board UI → batch + review queue →
OTel audit → Tier-2 profiling → codegen guard), with a Postgres checkpointer wired to the same
pgvector store the memory loop uses.

---

## 4. Architecture Overview

### Component Map

| Component | Role | Talks to |
|---|---|---|
| **Node registry** (`agents/registry.py`) | Declares the typed node catalogue (`source`, `sample`, `profile`, `contract`, `mask`, `publish`, `gate`, `batch`) + `validate_spec` | Planner, executor, composer palette |
| **IntentPlanner** (`agents/planner.py`) | NL intent → registry-validated `PipelineSpec` (few-shot grounded by recipes) | Registry, recipes, LiteLLM |
| **Recipes** (`agents/recipes.py`) | Golden `intent→plan` exemplars; planner few-shots + composer "start from a recipe" | Planner, registry |
| **Capability guard** (`agents/capability_guard.py`) | "Generate external code only if redibis can't already do it" | Codegen route |
| **Tool runner** (`agents/tool_runner.py`) | Binds each node kind to a deterministic core call (`_run_profile_scan`, `_run_pii_scan`, …) — **the glue** | Scan/profiling/masking/contracts/enrich/store/catalog |
| **LangGraphExecutor** (`agents/executor.py`) | Compiles a spec to a LangGraph, runs it with a checkpointer + `interrupt()` for HITL | Tool runner, lineage store |
| **BatchExecutor** (`agents/batch_executor.py`) | Per-table fan-out, shared checkpointer, resume | LangGraphExecutor, lineage store |
| **LineageStore** (`agents/lineage_store.py`) | Durable run records (`runs/<run_id>/run.json`), cancel tokens | Executor, board API |
| **Handoff** (`agents/handoff.py`) | Materialises one table's run into a manual `ScanSession` | session_manager, run folder |
| **CopilotKit bridge** (`agents/copilotkit_bridge.py`) | Optional AG-UI/LangGraph chat endpoint `/api/copilotkit/agent` | Planner, registry |

### System Diagram

```mermaid
graph TD
    CSV[CSV / Hive / Oracle sample] --> SRC[source / sample node]
    Intent[NL intent or composer] --> Planner[IntentPlanner]
    Recipes[Golden recipes] --> Planner
    Planner --> Spec[PipelineSpec - registry validated]
    Spec --> LG[LangGraphExecutor]
    SRC --> LG
    LG --> TR[tool_runner: bind node to core call]
    TR --> PROF[Profiler - GE/OM/YData]
    TR --> PII[PIIDetector + EquationEngine]
    TR --> MASK[MaskingEngine - mask/hash/FPE]
    TR --> ENR[EnrichmentService - LiteLLM]
    PROF --> CW[ScanContractWriter -> RunMerger]
    PII --> CW
    ENR --> CW
    CW --> CS[ContractStore.upsert - ONLY writer]
    LG --> GATE{Approval gate - interrupt}
    GATE -->|steward approves| PUB[catalog_push -> CatalogService]
    PUB --> OM[(OpenMetadata)]
    LG --> LIN[LineageStore - run.json]
    LIN --> BOARD[Composer / Dashboard / SSE]
    LG -.handoff one table.-> SESS[Manual 360 ScanSession]
    Chat[CopilotKit / AG-UI] --> Bridge[/api/copilotkit/agent] --> Planner
```

---

## 5. Core Concepts Glossary

**PipelineSpec**
: The validated graph of nodes + edges the executor runs. Produced by the planner or the composer; must pass `registry.validate_spec` (typed ports, required upstreams). It is the contract between "what the user asked" and "what runs."

**Node kind**
: One governed capability (`source`, `sample`, `profile`, `contract`, `mask`, `publish`, `gate`, `batch`). Each kind maps to exactly one deterministic core call in `tool_runner`. Adding a capability = registering a node, never raw library calls.

**Tool binding (the glue)**
: The `_run_*` functions in `tool_runner.py` that translate a node into a real redibis call and stash results in `TableRunState`/`state.extras`. This is where "agent" meets "engine."

**Detector vs equation (evidence vs verdict)**
: `PIIDetector.detect()` returns *evidence* (`detected=False`); `EquationEngine.decide()` sets the *verdict*. The LLM may propose but never sets verdicts. A core invariant — agents inherit it for free by calling the engine.

**ContractStore.upsert()**
: The single writer to the contracts bucket. Smart upsert + audit + PII-decision reconciliation + identity locking. Agents never write contracts directly — they assemble partials and merge through this.

**Durable checkpointer**
: LangGraph's persisted state so a run can resume exactly where it stopped (after a crash, or after a human approval). Backed by the same Postgres used for pgvector when `memory.enabled`.

**interrupt() / Command(resume=…)**
: LangGraph's human-in-the-loop primitive. The approval gate node calls `interrupt()`, the run becomes `AWAITING_HITL`, and `executor.resume()` continues it with `Command(resume=…)` after a steward decides.

**AgenticSession vs ScanSession**
: The executor attaches an **AgenticSession** (a manifest in the lineage store) and sets `AgentRun.session_id` to it — it never touches the manual `session_manager`. A real **ScanSession** is created only at handoff. This is why agent and manual contexts never clobber each other.

**Run vs job (table) ids**
: One `agent_run_id` (batch) fans out to many per-table scan `run_id`s. The manual console holds **one** table at a time; handoff maps a chosen table's `run_id` into a manual `session_id`.

**Capability guard**
: The policy that codegen produces *external* code only when no native node covers the intent (`decide_codegen`). Keeps the agent on the governed path by default.

**Suggest-only / gated autonomy**
: LLM-derived decisions default to "propose"; writes, external pushes, and irreversible actions pass an approval gate routed to a human role. The default posture of the whole layer.

**Egress validation**
: Before any codegen leaves the box, `prepare_codegen_request` + `hard_block` enforce data-residency/PII rules independent of the model provider.

---

## 6. Common Pitfalls

| Pitfall | Why It Happens | How to Avoid It |
|---|---|---|
| Letting the LLM "decide" PII/quality | Treating the model as the brain instead of the planner/proposer | Bind nodes to the detector+equation; the LLM only plans and enriches. Keep `detected=False` in the detector. |
| Writing contracts from agent code | Convenience: "just upsert here" | Always go through `ScanContractWriter` → `RunMerger` → `ContractStore.upsert()`. It's the only writer; it enforces identity + PII overlay. |
| Sharing one global session across agent + manual | Assuming "a session is a session" | Agent runs live in the lineage store as `AgenticSession`; only handoff mints a manual `ScanSession`. Never register agent sessions in `session_manager`. |
| Generating code for something native | The intent regex routes "mask"/"quality" to codegen | Run `decide_codegen` first; native overlap steers to the pipeline node. Extend `CAPABILITY_KEYWORDS` when adding capabilities. |
| Skipping the approval gate before publish | "It's just a demo" | Keep `gate` upstream of `publish`/`contract_write`. The executor enforces `state.approval_granted`; `interrupt()` is the seam. |
| Assuming the run is fast = it scanned | A skipped step (no data / engines missing) returns instantly | Read the step `reason`/Debug SSE; ensure a sample is resolvable (sample_dir / external source creds). |
| Pasting a static capability list into the planner prompt | It drifts from the engine | Build planner context live: `native_capability_surface()` + `catalog_for_planner()` + `recipes_for_intent()`. |

---

## 7. Ecosystem & Integration Map

The agentic layer sits between redibis's deterministic core and the outside world: it is driven
by **LangGraph** (orchestration) and optionally **CopilotKit/AG-UI** (chat), calls **LiteLLM**
for any model work, reads samples from **local files / Hive / Oracle**, and publishes governed
**ODCS v3** contracts to **OpenMetadata**. It runs inside the same **FastAPI** webapp as the
manual console and shares its **pgvector/Postgres** memory + checkpointer.

| Integration | Category | How It Connects | Notes |
|---|---|---|---|
| LangGraph | Orchestration | `executor.py` compiles `PipelineSpec`→graph; `interrupt()`/checkpointer | Core of the agent runtime |
| CopilotKit / AG-UI | Agentic UI | `/api/copilotkit/agent` (when `copilotkit_enabled`) via `ag-ui-langgraph` | Optional; board keeps REST fallback |
| LiteLLM | LLM gateway | `enrich/providers.py` `get_provider`; planner model | All model calls routed through `guarded_model_call` |
| Great Expectations | Profiling/quality | `profiling` registry + `quality_phase` | Default profiler engine |
| OpenMetadata | Catalog | `CatalogService.push()` from `catalog_push` node | Atlas planned |
| Presidio + GLiNER | PII detection | `PIIDetector`; equation engine decides | Evidence only |
| Postgres + pgvector | State + memory | LangGraph checkpointer + column-memory store | Enabled via `memory.dsn_ref` |
| FastAPI | Web/API host | Board + composer + SSE endpoints in `webapp/backend.py` | Same app as manual console |
| Hive / Oracle (JDBC) | Source | `external_sample.py` via metastore/JDBC | Sampling for remote tables |
| OpenTelemetry | Observability | spans per step + audit DAG | `telemetry/otel.py` |
