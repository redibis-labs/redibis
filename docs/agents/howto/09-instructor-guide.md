# 09 — Instructor guide (course delivery)

**Audience:** trainers building a workshop or internal certification track from this
how-to set.

---

## 1. How this set relates to engineer courses

| Track | Path | Focus |
|-------|------|-------|
| **Operator / steward (this set)** | `docs/agents/howto/` | Board UI, workflows, safety |
| **Engineer beginner → mastery** | `docs/agents/course/` | Package internals, LangGraph, registries |

Suggested pairing: run **howto 00–08** for all roles on day 1; send backend engineers to
**course beginner M1–M4** on day 2.

Update **course beginner M7** (“composer & board UI”) to assign howto 02–03 as pre-read.

---

## 2. Environment checklist (before class)

- [ ] Webapp on loopback; `REDIBIS_CONFIG` pointed at a lab YAML
- [ ] `agents.enabled: true`, `auto_approve_writes: false` for HITL demos
- [ ] Sample CSVs staged; at least one active contract table optional
- [ ] `[agents]` installed; optional `[copilotkit]` for chat demo
- [ ] Learners know `/agents` is typed, not in Scan nav
- [ ] No production credentials in Shared Settings

Example: `config/examples/agents-local-full.yaml` with auto-approve forced false for class.

---

## 3. Timing templates

### Half-day (4 hours)

00 (30) → 01 (45) → 02 (60) → 03 (45) → Recipe A or B (40) → debrief (20)

### Two-day operator

**Day 1:** 00–04 + Recipe A/B  
**Day 2:** 05–07 + Recipes C/D/E + capstone rubric

---

## 4. Demo script (15 minutes)

1. Open `/agents` — show missing nav on `/`.
2. Ask one sentence with CSV — show runtime panel.
3. Composer — validate a tiny graph.
4. Flip to Results — point at `run_id`.
5. Show codegen guard with a native intent.
6. Show Settings role matrix (no secrets).

---

## 5. Assessment ideas

- Short quiz: four tabs’ purposes; HITL vs auto-approve; handoff vs agent run id.
- Practical: Recipe B with screen recording or checklist sign-off.
- Advanced: document a lock-down YAML for a shared VM.

---

## 6. Keeping docs honest

When UI labels change, update the matching howto file **and** this index’s capability map.
Prefer linking live CLI (`redibis agents nodes`) over pasting stale node tables.

Status of agent defaults: see `AgentsConfig` in `redibis/config.py` and
[`CODEGEN_SERVICE.md`](../../CODEGEN_SERVICE.md) §2.1.

---

## 7. Out of scope for this course

- Writing new registry nodes (engineer course)
- Deploying enterprise Cloud Run codegen (vendor ops docs)
- Free-text PII playground on Scan (`TEXT_PII_API_CLI_TUTORIAL.md`) — separate module
