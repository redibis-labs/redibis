# 00 — Getting started with the Agentic board

**Goal:** open `/agents`, understand what you are looking at, and verify the runtime.  
**Time:** 30–45 minutes.  
**Prerequisites:** redibis webapp running; optional sample CSV (e.g. telecom customers).

---

## 1. Open the board

Type the path in the browser (it is **not** linked from Scan / Settings / Contracts):

```text
/agents
```

You should see the **Ask** headline: “What should we do with your data?” and nav tabs:

| Tab | Purpose |
|-----|---------|
| **Ask** | Natural language → plan → run |
| **Composer** | Visual pipeline editor |
| **Dashboard** | Multi-table / batch progress |
| **Results** | Per-table cards, HITL, artifacts, handoff |

External links on the board: Scan, Contracts, Reports, Settings.

If the page shows a disabled / blocked banner, `agents.enabled` is false — see §3.

---

## 2. Install extras (once)

```bash
pip install -e ".[agents]" -c requirements/constraints.txt
# optional chat panel
pip install -e ".[agents,copilotkit]" -c requirements/constraints.txt
```

Without `[agents]`, LangGraph HITL may fall back to legacy loops.

---

## 3. Config you must know

Defaults favor a **local lab**:

```yaml
agents:
  enabled: true
  auto_approve_writes: true      # set false for steward gates
  dynamic_sandbox_enabled: true
  copilotkit_enabled: true
  allow_external_codegen: true   # still needs URL + token for remote
  planner_provider: ""           # empty = heuristic plan (no LLM)
  codegen_service_url: ""        # empty = local template / local LLM codegen
  runs_dir: ./agent_runs
```

Point the process at your file and restart:

```bash
export REDIBIS_CONFIG=/absolute/path/to/redibis.yaml
./scripts/webapp.sh --restart
```

Confirm under **Settings** that agents show as enabled. Prefer binding the webapp to
`127.0.0.1` when auto-approve / sandbox are on.

Full local stack example: `config/examples/agents-local-full.yaml`.

---

## 4. First checklist (lab)

1. Open `/agents` → Ask loads.
2. Open **AI runtime** panel — note planner method (heuristic vs LLM) and providers.
3. Switch to **Composer** — palette of nodes appears (may take a moment to load).
4. Open **Settings** → LLM / agent section — role matrix is for *new* runs only.
5. Confirm `agent_runs/` (or your `runs_dir`) is writable.

---

## 5. Mental model

```text
Ask / Composer  →  PipelineSpec (validated nodes)
        ↓
   LangGraph executor (or legacy loop)
        ↓
   Deterministic tools (profile / PII / quality / contract …)
        ↓
   Lineage under agents.runs_dir  +  optional handoff to Scan console
```

The agent **proposes**; irreversible contract writes and external codegen are gated by
config and HITL (when auto-approve is off). Contract writes still go only through
`ContractStore.upsert()` in the core.

---

## 6. Lab exercise

1. Start webapp with default agents config.
2. Screenshot or note the Ask runtime panel fields.
3. Toggle `agents.enabled: false`, restart, confirm Send is blocked; restore `true`.

**Pass criteria:** you can open `/agents`, name the four main tabs, and explain why
the Ask nav is missing from the Scan console.

---

## Next

→ [01 — Ask](01-ask.md)
