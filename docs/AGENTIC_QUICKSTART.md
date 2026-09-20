# Agentic Quickstart — Running Redibis with an Agentic YAML Config

**Audience:** operators and engineers starting the agentic pipeline for the first time.
**Board how-tos (Ask / Composer / Dashboard / Results / codegen):**
[`docs/agents/howto/`](agents/howto/README.md).
**Companion:** `docs/CODEGEN_SERVICE.md` (hosted codegen pointer),
`docs/AGENTIC_WEB_EXPERIENCE.md`, `docs/AGENTIC_PAGE_HELP.md`, `AGENTS.md` (governance rules).

Everything below is driven by the `agents:` block of `redibis.yaml` — the same config
spine used by scans. Agentic mode is **on by default** (including CopilotKit, sandbox,
auto-approve writes, and external-codegen permission). Bind to loopback for local use,
or set the open flags `false` for locked-down hosts.

---

## 1. Install

```bash
pip install -e ".[agents]" -c requirements/constraints.txt   # langgraph durable execution
pip install -e ".[agents,copilotkit]"                        # + AG-UI chat endpoint
```

| Extra | Adds | Needed for |
|---|---|---|
| `agents` | `langgraph`, `langgraph-checkpoint` | durable HITL execution, per-table checkpoints |
| `copilotkit` | `ag-ui-langgraph` | `/api/copilotkit/agent` chat endpoint |

Without `[agents]`, set the executors to the legacy loops (`batch` / `sequential`) —
see §2. Verify:

```bash
redibis agents nodes --json      # pipeline node registry
redibis agents plugins           # entry-point agent tool plugins
```

## 2. Minimal agentic `redibis.yaml`

```yaml
# redibis.yaml — minimal agentic setup
table: ""                       # set per run, or pass --tables
scan_types: [profile, quality, pii]

storage:
  # your existing contract/runs bucket config

agents:
  enabled: true                 # master switch (default true)
  runs_dir: ./agent_runs        # run ledgers, traces, artifacts
  sample_dir: ./samples         # per-table sample CSVs for scan steps
  auto_approve_writes: true     # default on; set false for HITL contract writes
  single_table_executor: langgraph   # langgraph (durable HITL) | batch (legacy)
  batch_executor: langgraph          # langgraph (per-table checkpoint) | sequential
  max_retries: 2                # output-validator smart-retry budget
  max_plan_repairs: 2           # planner structural repair attempts
```

Point redibis at it:

```bash
export REDIBIS_CONFIG=/path/to/redibis.yaml
# or pass --config on every agents subcommand
```

## 3. Full `agents:` reference

```yaml
agents:
  # ── lifecycle ────────────────────────────────────────────────────────────
  enabled: true                 # master switch (default on)
  runs_dir: ./agent_runs        # where run ledgers/traces/artifacts land
  sample_dir: ""                # dir of per-table sample CSV/Parquet

  # ── execution engine ─────────────────────────────────────────────────────
  single_table_executor: langgraph   # durable HITL vs "batch" legacy loop
  batch_executor: langgraph          # per-table checkpoint vs "sequential"
  max_retries: 2                     # enrich/contract critique retry budget
  max_plan_repairs: 2                # planner repairs when validate_spec fails

  # ── human-in-the-loop ────────────────────────────────────────────────────
  auto_approve_writes: true     # default on (local lab); false = HITL gates

  # ── dynamic tools ────────────────────────────────────────────────────────
  dynamic_tools_dir: ./dynamic_tools
  dynamic_sandbox_enabled: true      # default on; false = register-only stub

  # ── codegen (see CODEGEN_SERVICE.md) ────────────────────────────────
  allow_external_codegen: true       # default on; still needs codegen_service_url
  codegen_default_target: ranger     # ranger | (future targets)
  codegen_service_url: ""            # Cloud Run URL; empty = local codegen only
  codegen_provenance_secret: ""      # HMAC secret shared with the service

  # ── chat / planner ───────────────────────────────────────────────────────
  copilotkit_enabled: true      # /api/copilotkit/agent (requires [copilotkit])
  planner_provider: ""          # empty = heuristic plan; set for IntentPlanner LLM
  planner_model: ""
```

**Production lock-down:** set `auto_approve_writes`, `dynamic_sandbox_enabled`,
and `allow_external_codegen` to `false` on shared hosts (and bind behind auth).
The `/agents` page still requires a dashboard login
([`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md)). Local defaults favor a working lab
stack; each flag still opens a distinct trust boundary.

## 4. Your first agentic run

### 4.1 Inspect the pipeline vocabulary

```bash
redibis agents nodes            # available pipeline nodes
redibis agents example          # example pipeline spec + compiled prompt-plan
```

### 4.2 Write a pipeline spec

```yaml
# pipelines/telecom-daily.yaml
name: telecom-daily
description: Profile → quality → PII → contract merge for telecom tables.
steps:
  - node: profile
  - node: quality
  - node: pii
  - node: contract_merge
    requires_approval: true      # HITL gate before any contract write
```

Compile it to a prompt-plan before running anything:

```bash
redibis agents compile --pipeline-file pipelines/telecom-daily.yaml --show-diff
redibis agents docgen --pipeline-file pipelines/telecom-daily.yaml -o docs/pipeline.md
```

### 4.3 Execute one table (start here)

```bash
redibis agents execute telecom.customers \
  --config redibis.yaml \
  --pipeline-file pipelines/telecom-daily.yaml \
  --sample ./samples/telecom_customers.csv
```

### 4.4 Batch across tables

```bash
redibis agents run \
  --config redibis.yaml \
  --pipeline-file pipelines/telecom-daily.yaml \
  --tables telecom.customers,telecom.cdr \
  --sample-dir ./samples \
  --dry-run                     # catalog push dry-run; drop when satisfied
```

### 4.5 Observe

```bash
redibis agents runs --limit 20            # recent runs
redibis agents status <run_id>            # status + decision ledger
redibis agents trace <run_id> --json      # DAG trace (React Flow JSON)
redibis agents cancel <run_id> --reason "wrong sample"
```

Run ledgers, artifacts, and traces are written under `agents.runs_dir`. The web
Agentic board renders the same data — see `docs/AGENTIC_WEB_EXPERIENCE.md`.

## 5. Optional: chat / planner

```yaml
agents:
  copilotkit_enabled: true
  planner_provider: gemini        # any provider in llm_providers.json
  planner_model: gemini-2.0-flash
```

Requires `pip install -e ".[copilotkit]"`. Exposes `/api/copilotkit/agent`; the planner
also backs `POST /api/agents/plan` when the request body omits a provider. Every model
call still routes through `guarded_model_call` (RAI/residency gate) — no exceptions.

## 6. Trust boundaries — read before enabling anything

| Switch | What it permits | Guard rails |
|---|---|---|
| `auto_approve_writes: true` | agent merges contracts without human review | contracts still go through `ContractStore.upsert()`; decision overlays still win. Prefer `requires_approval` gates in the pipeline spec instead |
| `dynamic_sandbox_enabled: true` | executes approved dynamic tools | tools must be proposed → scanned → approved first; sandbox is bounded. Generated code is never executed on proposal |
| `allow_external_codegen: true` | metadata leaves the trust boundary | `EgressGate` scrubs intent text, blocks raw-PII signals with non-local residency, and rejects contract summaries carrying `sample`/`description` fields |
| `copilotkit_enabled: true` | interactive LLM surface | `guarded_model_call` + planner validation; LLM output remains a proposal |

Nothing here bypasses the invariants in `CLAUDE.md`: `ContractStore.upsert()` stays the
only contract writer, LLM output is a proposal until approved, and no raw cell values
leave the deployment.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `agents` commands run but nothing executes | `agents.enabled: false`, or `REDIBIS_CONFIG` not pointing at your file |
| `langgraph` import error | install `[agents]`, or set both executors to `batch` / `sequential` |
| Codegen returns unexpected status | Local engine needs no URL. For remote: set `agents.codegen_mode: remote`, `agents.codegen_service_url`, and `REDIBIS_CODEGEN_TOKEN`. Hosted GCP service is vendor-only under `enterprise/services/codegen/` |
| Scan steps skipped | no sample for the table: set `sample_dir`, `--sample`, or `--sample-paths table=path` |
| Chat endpoint 404 | `copilotkit_enabled: false` or `[copilotkit]` extra not installed |
| Run stuck awaiting approval | expected with HITL gates — approve in the web board or via the approval API |

## 8. Next steps

- Hosted codegen: `docs/CODEGEN_SERVICE.md`
- Behavior tuning without code edits: `docs/BEHAVIOR_POLICY_ARCHITECTURE.md`
- Portable config bundles: `docs/REDIBIS_PACK_DESIGN.md`
- Governance rules for agents: `AGENTS.md`
