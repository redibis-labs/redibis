# Agentic Board How-To — Course & documentation index

**Audience:** operators, data stewards, and instructors who use the `/agents` board.  
**Purpose:** teach every board capability as runnable how-tos. Use this set as the
**student workbook** for a course and as the canonical **how-to documentation**.

Engineer deep-dives (registry, LangGraph internals) live in
[`docs/agents/course/`](../course/). Governance design lives in
[`AGENTIC_GOVERNANCE_DESIGN.md`](../AGENTIC_GOVERNANCE_DESIGN.md).

---

## How to open the board

There is **no Ask / Agents link in the main Scan nav**. Type the URL:

```text
http://127.0.0.1:8000/agents
```

(Use your webapp host/port.) Sign in first — `/agents` is not public.
See [`DASHBOARD_AUTH.md`](../../DASHBOARD_AUTH.md). Agents are **on by default**
in open-core; set `agents.enabled: false` in `REDIBIS_CONFIG` to block execution.

---

## Reading order (course path)

| # | Guide | Board area | Lab time |
|---|--------|------------|----------|
| 00 | [Getting started](00-getting-started.md) | Open board, config, first check | 30–45 min |
| 01 | [Ask](01-ask.md) | Natural-language runs | 45–60 min |
| 02 | [Composer](02-composer.md) | Visual pipeline graph | 60–90 min |
| 03 | [Dashboard & Results](03-dashboard-and-results.md) | Batch progress, HITL, handoff | 60 min |
| 04 | [Review & audit](04-review-and-audit.md) | Steward queue, traces, artifacts | 45 min |
| 05 | [Policy codegen](05-codegen.md) | Propose Ranger / external policies | 45 min |
| 06 | [Planner, chat & LLM roles](06-planner-chat-llm.md) | Heuristic vs LLM plan, CopilotKit, Settings roles | 45 min |
| 07 | [Safety switches](07-safety-and-settings.md) | Auto-approve, sandbox, RAI, loopback | 30 min |
| 08 | [End-to-end recipes](08-recipes.md) | Capstone workflows | 90–120 min |
| 09 | [Instructor guide](09-instructor-guide.md) | Teaching plan, rubrics, demos | — |

**Crash path (half day):** 00 → 01 → 02 → 03 → one recipe from 08.  
**Full operator path (1.5–2 days):** all modules in order.

---

## Capability → guide map

| You want to… | Open | Read |
|--------------|------|------|
| Describe a goal in plain language | **Ask** | [01](01-ask.md) |
| Drag nodes, validate graph, preview scope | **Composer** | [02](02-composer.md) |
| Watch multi-table progress | **Dashboard** | [03](03-dashboard-and-results.md) |
| Inspect per-table cards, approve a gate | **Results** | [03](03-dashboard-and-results.md) |
| Open steward review items | Review (from Results/Dashboard) | [04](04-review-and-audit.md) |
| Propose Ranger (or similar) policy text | Codegen (board tab) | [05](05-codegen.md) |
| Chat / AG-UI panel | Ask (+ CopilotKit when enabled) | [06](06-planner-chat-llm.md) |
| Lock down a shared host | Settings + YAML | [07](07-safety-and-settings.md) |
| Hand a table to the manual Scan console | Results → Handoff | [03](03-dashboard-and-results.md), [SESSIONS_AND_HANDOFF](../SESSIONS_AND_HANDOFF.md) |

---

## Related docs (not duplicated here)

| Doc | Role |
|-----|------|
| [`AGENTIC_QUICKSTART.md`](../../AGENTIC_QUICKSTART.md) | YAML install + CLI `redibis agents …` |
| [`AGENTIC_PAGE_HELP.md`](../../AGENTIC_PAGE_HELP.md) | Status-panel meanings |
| [`AGENTIC_WEB_EXPERIENCE.md`](../../AGENTIC_WEB_EXPERIENCE.md) | Routes + which steps call models |
| [`REDIBIS_CAPABILITY_MAP.md`](../REDIBIS_CAPABILITY_MAP.md) | Native nodes vs external codegen guard |
| [`TEXT_PII_API_CLI_TUTORIAL.md`](../../TEXT_PII_API_CLI_TUTORIAL.md) | Free-text PII scan / de-id (Scan console, not Ask board) |
| [`TEXT_PII_EVAL.md`](../../TEXT_PII_EVAL.md) | Span evaluation (strict vs value F1, match classes) |
| [`TEXT_PII_EVAL_TUTORIAL.md`](../../tutorials/TEXT_PII_EVAL_TUTORIAL.md) | Hands-on eval CLI / UI / API |
| [`enterprise/README.md`](../../CODEGEN_SERVICE.md) | Hosted codegen / commercial add-ons |

---

## Course outcomes

After this series a learner can:

1. Open `/agents` and confirm execution is allowed.
2. Run a single-table Ask flow with a sample CSV.
3. Author and validate a Composer graph, then execute with preview.
4. Resume a human-in-the-loop gate and hand off to manual Scan.
5. Explain when codegen is refused because a native capability already covers the ask.
6. Name the safety flags that must be off on a shared production host.
