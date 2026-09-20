# 08 — End-to-end recipes (capstones)

**Goal:** practice complete steward workflows on the board.  
**Time:** 90–120 minutes (pick 2–3 recipes).

Use fixtures under `tests/` / `tests/fixtures/` or your own CSV. Set
`auto_approve_writes: false` for any recipe that includes a gate.

---

## Recipe A — Single CSV via Ask

1. Ask → attach CSV → “Profile and detect PII.”
2. Results → open PII/profile artifacts.
3. Handoff → Scan console → approve columns into the basket (manual).

**Teaches:** Ask, artifacts, handoff boundary.

---

## Recipe B — Composer with steward gate

1. Composer: sample → profile → contract (PII) → gate → (optional) publish dry-run.
2. Validate → Preview one table → Run.
3. Results → Approve HITL → confirm completion.

**Teaches:** graph authorship, HITL, auto-approve off.

---

## Recipe C — Two-table Dashboard

1. Ensure two active contracts (or batch tables) exist.
2. Composer/Ask batch both tables with a gate.
3. Dashboard: watch first pause; resume; watch second.

**Teaches:** batch_meta, per-table pause, resume API behavior.

---

## Recipe D — Native vs codegen

1. Intent “detect PII on telecom.customers” via codegen submit → expect native steer.
2. Intent “Generate Ranger masking policy JSON from maskingPolicy” → proposed artifact.
3. Discuss why enforcement is still external.

**Teaches:** capability guard ([05](05-codegen.md)).

---

## Recipe E — Heuristic vs LLM plan

1. Empty planner → Ask → note heuristic.
2. Configure local planner provider → same prompt → note LLM + any repair.
3. Compare resulting graphs (node kinds).

**Teaches:** [06](06-planner-chat-llm.md).

---

## Recipe F — Disabled agents

1. `agents.enabled: false`, restart.
2. Open `/agents` — UI visible, Send/Run blocked.
3. Restore enabled.

**Teaches:** discoverability vs execution.

---

## Capstone rubric (instructor)

| Criterion | Points |
|-----------|--------|
| Correct tab for the task | 10 |
| Validated graph or successful Ask plan | 25 |
| HITL handled when required | 25 |
| Handoff or artifact inspection | 20 |
| Explains codegen vs native | 20 |

---

## Next

→ [09 — Instructor guide](09-instructor-guide.md)
