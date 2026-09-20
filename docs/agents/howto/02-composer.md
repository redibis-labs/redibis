# 02 — Composer (visual pipeline)

**Goal:** build, validate, preview, and execute a pipeline graph without relying on NL.  
**Time:** 60–90 minutes.  
**Tab:** **Composer**

---

## 1. What Composer is for

Composer is the **pipeline board**: a palette of registered nodes, a canvas, config cards,
intent bar (optional NL → plan into the graph), validate, preview scope, and run.

Use it when you need:

- Explicit **gates** before publish / contract write
- A repeatable graph for demos or training
- Fine-grained node params (profiler engine, sample strategy, etc.)

---

## 2. UI tour

| Area | Role |
|------|------|
| **Palette** | Nodes from the live registry (`profile`, `pii`, `quality`, `contract`, `gate`, …) |
| **Canvas** | Drag nodes; connect edges that match typed ports |
| **Config card** | Params for the selected node |
| **Intent / plan** | Optional NL fill of the graph (same planner stack as Ask) |
| **Validate** | `validate_spec` errors without executing |
| **Preview** | Which tables / scope will run |
| **Run** | Start execution (respects `agents.enabled`) |

Chat panel (CopilotKit) may appear when `copilotkit_enabled` and the extra are installed.

---

## 3. Core node vocabulary

| Node / flag | What learners should remember |
|-------------|-------------------------------|
| `source` / `sample` | Bring data in; bound the sample |
| `profile` | Structural stats |
| `contract` + `pii` / `quality` / `classify` | ODCS partials / full merge path |
| `mask` | Suggest strategies (enforcement is external) |
| `gate` | Human approval pause (when auto-approve is off) |
| `publish` | Push to catalog (often dry-run first) |

Authoritative live list: `redibis agents nodes` or the palette. Capability phrases:
[`REDIBIS_CAPABILITY_MAP.md`](../REDIBIS_CAPABILITY_MAP.md).

---

## 4. Workflow (happy path)

1. Clear or load a starter graph.
2. Add `sample` → `profile` → `contract` (enable PII and/or quality as needed).
3. Optionally insert a `gate` before publish.
4. **Validate** until clean.
5. **Preview** tables (try-on-N for batch).
6. **Confirm run** → follow on **Dashboard** / **Results**.

---

## 5. Common validate failures

- Missing required upstream port (e.g. contract needs profile).
- Edge to a node kind that does not accept that input.
- Empty table list for batch execute.

Fix the graph; do not bypass validation.

---

## 6. Lab exercise

1. Build: sample → profile → contract with PII on.
2. Validate successfully.
3. Preview for one table; run.
4. Break an edge on purpose; confirm Validate explains the error; fix it.

**Pass criteria:** a self-authored graph validates and produces a run with at least one
completed profile or PII step.

---

## Next

→ [03 — Dashboard & Results](03-dashboard-and-results.md)
