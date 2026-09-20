# 01 — Ask (natural-language runs)

**Goal:** describe a data-governance task in plain language, attach samples, and start a run.  
**Time:** 45–60 minutes.  
**Tab:** **Ask**

---

## 1. What Ask is for

Ask turns a sentence into a **validated pipeline** (via heuristic planner or LLM
IntentPlanner), then executes it. Prefer Ask when:

- You know the *goal* (“profile and find PII on this CSV”) but not the node graph.
- You want a quick single-table or small-batch run with optional file attachments.

Prefer **Composer** when you need exact node order, gates, or reusable graphs.

---

## 2. UI tour

| Control | Role |
|---------|------|
| Prompt textarea | Intent in natural language |
| **+** | Attach `.csv` / `.parquet` samples |
| **Send** | Plan + start (blocked if agents disabled) |
| **AI runtime** | Effective planner / enrichment / RAI config for *new* runs |
| Debug / steps / log | Live progress once a run starts |
| Artifacts | Links to outputs when available |

Clarifying questions may appear if the planner needs a table name or scope.

---

## 3. Good prompts (patterns)

```text
Profile telecom.customers and detect PII.
```

```text
Run quality expectations on the attached sample and draft a contract.
```

```text
Classify columns for telecom.customers using the telecom policy pack.
```

Avoid asking Ask to “write a Python script to mask emails” when native **mask** /
**PII** / **contract** nodes already cover it — the capability guard steers you to
native steps (see [05 — Codegen](05-codegen.md)).

---

## 4. Samples and tables

- Attach files with **+**, or ensure tables already exist as active contracts / catalog.
- Named tables like `telecom.customers` resolve against the contract store when present.
- Multi-table asks can open the **Dashboard** for progress.

---

## 5. What happens after Send

1. Intent → `PipelineSpec` (heuristic or LLM; see [06](06-planner-chat-llm.md)).
2. `validate_spec` — invalid graphs fail before execution.
3. Executor runs nodes; status streams into Debug / log.
4. You jump to **Results** or **Dashboard** for the `run_id`.

Run ledgers live under `agents.runs_dir` (default `./agent_runs`).

---

## 6. Lab exercise

1. Attach a small CSV (or use a fixture under `tests/`).
2. Prompt: “Profile this sample and detect PII.”
3. Send; watch runtime + steps.
4. Open **Results** for the run; note table status chips.

**Pass criteria:** a run reaches a terminal status (`completed` / `awaiting_hitl` /
`failed`) and you can find its `run_id` on Dashboard or Results.

---

## Next

→ [02 — Composer](02-composer.md)
