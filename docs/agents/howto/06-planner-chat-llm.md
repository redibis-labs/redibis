# 06 — Planner, chat & LLM capability roles

**Goal:** know when the board uses a heuristic vs an LLM, and how Settings roles bind.  
**Time:** 45 minutes.

---

## 1. Planner modes

| Mode | When | Behavior |
|------|------|----------|
| **Heuristic** | `planner_provider` empty (default) | Deterministic NL → rough `PipelineSpec` |
| **LLM IntentPlanner** | Provider + model configured | Plans via `guarded_model_call` + repair loop |

Precedence (Ask / Composer / Copilot): YAML `agents.planner_*` → Settings
`agentic_defaults` → heuristic fallback. The UI reports the winning source.

---

## 2. CopilotKit / AG-UI chat

When `copilotkit_enabled: true` and `pip install redibis[copilotkit]`:

- Endpoint: `/api/copilotkit/agent`
- Board may show a chat panel for planning assistance

Chat does **not** replace Composer validation. Always Validate before Run.

---

## 3. Capability routing (Settings LLM matrix)

Roles such as `agent.planner`, `agent.copilot`, `contract.enrichment`,
`codegen.generator` bind **provider + model** for *new* runs. Snapshots freeze routing
for in-flight work.

- Credential refs only — never paste raw API keys into shared settings files.
- Hosted GCP codegen is **not** a LiteLLM role; it is HTTP egress / enterprise service.

See `docs/LLM_CAPABILITY_ROUTING_INDEX.md` (if present in your branch) and Settings → LLM.

---

## 4. Which board actions call models?

| Action | Model? |
|--------|--------|
| Heuristic plan | No |
| LLM plan / repair | Yes (`agent.planner*`) |
| Profile / PII equation / quality GE | No (deterministic) |
| Contract enrich node | Yes if enrich configured |
| Local codegen LLM path | Optional local only |
| Remote codegen | Vendor service (separate) |

---

## 5. Lab exercise

1. With empty `planner_provider`, Ask a simple prompt; note “heuristic” in runtime/debug.
2. Configure a local provider (demo/Ollama/SGLang) for `agent.planner`; Ask again; note LLM.
3. If CopilotKit is on, send one chat message; confirm Validate still required before run.

**Pass criteria:** learner can predict whether a given click will call a model.

---

## Next

→ [07 — Safety & settings](07-safety-and-settings.md)
