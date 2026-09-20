# 07 — Safety switches & Settings

**Goal:** configure a safe classroom vs a locked-down shared host.  
**Time:** 30 minutes.  
**Surfaces:** `/settings`, `REDIBIS_CONFIG`, startup warnings

---

## 1. Default posture (local lab)

Open-core defaults (revised):

| Flag | Default | Teaching note |
|------|---------|---------------|
| `agents.enabled` | `true` | Master execution switch |
| `auto_approve_writes` | `true` | **Turn false** for HITL labs |
| `dynamic_sandbox_enabled` | `true` | Approved dynamic tools may execute in-process |
| `copilotkit_enabled` | `true` | Chat endpoint when extra installed |
| `allow_external_codegen` | `true` | Still needs remote URL/token to leave the box |

Prefer **loopback bind** (`127.0.0.1`) when open flags are on. The webapp logs a warning
at startup when these are active.

---

## 2. Shared / production classroom lock-down

```yaml
agents:
  enabled: true
  auto_approve_writes: false
  dynamic_sandbox_enabled: false
  allow_external_codegen: false
  copilotkit_enabled: false
  planner_provider: ""    # or a private local endpoint only
```

Plus auth in front of the webapp (out of scope for open-core). Explicit
`agents.enabled: false` freezes execution for demos of the disabled state.

---

## 3. Settings page

- Hot LLM / planner defaults apply to **new** runs.
- Deployment fields marked restart-required must change in YAML + process restart.
- Secrets are redacted (`dsn_configured`, etc. — never raw keys).

---

## 4. Dynamic tools (advanced)

Operators can register dynamic tools; sandbox must be on for bounded execution after
approval. Teach: registration ≠ execution; AST validation ≠ container isolation.

---

## 5. Lab exercise

1. Run once with auto-approve on (no HITL).
2. Flip `auto_approve_writes: false`, restart, run a gated graph; complete HITL.
3. Document which three flags you would disable before exposing port `0.0.0.0`.

**Pass criteria:** written lock-down checklist matches §2.

---

## Next

→ [08 — End-to-end recipes](08-recipes.md)
