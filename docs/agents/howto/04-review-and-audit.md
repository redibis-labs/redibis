# 04 — Review queue & audit trail

**Goal:** use steward review items and inspect what a run actually did.  
**Time:** 45 minutes.  
**Surfaces:** Review (from board), audit / trace / artifacts APIs

---

## 1. Review queue

The board can surface a **review queue** of pending steward actions: HITL interrupts,
failed steps, or items waiting for human judgment. Typical actions:

- Jump to the paused table on Results
- Approve / reject the interrupt
- Open linked artifacts (PII / quality HTML)

Exact chrome varies by run shape; if nothing is pending, the queue is empty — that is
success for auto-approved lab runs.

---

## 2. Audit and traces

For a `run_id`:

| Concern | Where |
|---------|--------|
| Step records | Results summary / run JSON under `runs_dir` |
| Audit DAG / trace | Board audit view or `GET /api/agents/runs/{id}/audit` · `/trace` |
| Artifacts list | Results pane · `GET /api/agents/runs/{id}/artifacts` |
| Live log | Results stream · `GET /api/agents/runs/{id}/stream` |

Teach learners: **never trust the chat transcript alone** — the lineage store is the
source of truth for what ran.

---

## 3. Model-call transparency

Ask and run metadata can list **model calls** associated with the run (planner,
enrichment): provider, model, purpose, RAI outcome, fallback reason. Use this when
teaching “which capability used an LLM?”

See [`AGENTIC_WEB_EXPERIENCE.md`](../../AGENTIC_WEB_EXPERIENCE.md).

---

## 4. Lab exercise

1. Complete any Ask or Composer run.
2. Locate `agent_runs/<run_id>/` (or configured `runs_dir`) on disk.
3. From Results, open at least one artifact (HTML or YAML).
4. Call or view audit/trace and list two step names that executed.

**Pass criteria:** learner can point to lineage on disk and one API/UI audit surface.

---

## Next

→ [05 — Policy codegen](05-codegen.md)
