# Agentic page help

Operator how-tos for every board tab (course-ready):
**[`docs/agents/howto/`](agents/howto/README.md)**.

The `/agents` page is opened by typing the URL (not linked from the main Scan nav).
Agent execution defaults **on**; set `agents.enabled: false` to block runs while still
allowing inspection of the UI.

**Codegen:** [`CODEGEN_SERVICE.md`](CODEGEN_SERVICE.md) · agentic YAML:
[`AGENTIC_QUICKSTART.md`](AGENTIC_QUICKSTART.md).

## Understanding the status panel

| Setting | What the displayed value means |
|---|---|
| **Restart required** | These are deployment settings. The web Settings page shows their effective values but cannot change them. Edit the YAML selected by `REDIBIS_CONFIG`, then restart the web process. |
| **enabled: off** | The master agent execution switch, `agents.enabled`, is false. You can inspect and compose plans, but execution is blocked. |
| **single table executor: langgraph** | LangGraph is the selected backend for one-table runs. This is a backend choice, not an on/off status. It provides durable human-review interrupts when the optional dependency is installed. |
| **batch executor: langgraph** | LangGraph is selected for multi-table runs, with a checkpoint per table. This is also a backend choice, not an on/off status. |
| **copilotkit enabled: off** | The optional AG-UI/CopilotKit chat endpoint and board chat panel are disabled. The normal Agentic Ask and Composer REST flows do not require this switch. |
| **allow external codegen: off** | Contract metadata cannot be sent to an external code-generation service. This is deliberately opt-in because it permits external egress. |
| **codegen service configured: off** | `agents.codegen_service_url` is empty. Setting the URL configures a service; it does not by itself permit egress. Both this and `allow_external_codegen` are required for hosted codegen. |
| **dynamic sandbox enabled: off** | Approved dynamic tool source can be registered, but cannot execute. Enabling it permits bounded in-process execution after AST validation; it is not an OS/container security boundary. |

If `langgraph` is selected but the agent extra is not installed, execution can
fall back to the legacy sequential backend. Install the extra to get the
selected LangGraph behavior.

## Turn on the core agentic page

Most day-to-day Ask / Composer work only needs the master switch and LangGraph:

1. Install the LangGraph dependencies:

   ```bash
   pip install -e ".[agents]" -c requirements/constraints.txt
   ```

2. Add the following to your Redibis YAML. The repository example
   `config/examples/agents-docker.yaml` is also a valid starting point.

   ```yaml
   agents:
     enabled: true
     single_table_executor: langgraph
     batch_executor: langgraph
     copilotkit_enabled: false
     allow_external_codegen: false
     dynamic_sandbox_enabled: false
   ```

3. Select that file before starting Redibis:

   ```bash
   export REDIBIS_CONFIG=/absolute/path/to/redibis.yaml
   ./scripts/webapp.sh --restart
   ```

   If you launch the server another way, stop and restart that process while
   preserving `REDIBIS_CONFIG`. For Docker, the path must exist inside the
   container and be mounted there.

4. Open `/settings` and confirm **enabled** is **on**, then return to `/agents`.

Leave CopilotKit, external codegen, and the dynamic sandbox **on** by default
(local lab posture). Set them `false` in `REDIBIS_CONFIG` for shared or
network-exposed hosts.

## Full example: enable all agentic features

Use this profile only in a **lab / trusted environment**. It turns on every
capability the Settings status panel reports, plus the supporting memory and
planner wiring that make LangGraph HITL and LLM planning useful.

### 1. Install extras

```bash
pip install -e ".[agents,copilotkit,memory]" -c requirements/constraints.txt
```

- `agents` — LangGraph executor (single-table + batch)
- `copilotkit` — AG-UI endpoint at `/api/copilotkit/agent`
- `memory` — Postgres/pgvector for durable checkpoints across restarts

### 2. Deploy or run the codegen service (optional but required for “configured: on”)

Full readiness review + runbook + production plan:
**[CODEGEN_SERVICE.md](CODEGEN_SERVICE.md)**.

Quick local scaffold:

Quick local scaffold (vendor tree):

```bash
docker build -f enterprise/deploy/docker/codegen/Dockerfile -t redibis-codegen .
docker run --rm -p 8081:8081 \
  -e REDIBIS_CODEGEN_TOKEN=dev-secret \
  redibis-codegen
```

OSS local codegen needs no container — `submit_codegen()` defaults to
`redibis.agents.codegen_local`.

### 3. Environment variables

```bash
export REDIBIS_CONFIG=/absolute/path/to/redibis-agents-full.yaml
export REDIBIS_MEMORY_DSN='postgresql://user:pass@localhost:5432/redibis'
export REDIBIS_CODEGEN_TOKEN=dev-secret

# Planner / enrichment LLM — pick one provider from llm_providers.json
export GEMINI_API_KEY=...          # if planner_provider: gemini
# or: VLLM_API_KEY / Ollama needs no key when local
```

### 4. Complete YAML (`redibis-agents-full.yaml`)

```yaml
# Full agentic lab profile — enables every Settings "agents" status switch.
# Do not use as a production default without reviewing RAI / egress policy.

table: telecom.customers

scan_types:
  - profile
  - quality
  - pii

agents:
  enabled: true
  runs_dir: ./agent_runs
  sample_dir: ./tests/data
  dynamic_tools_dir: ./dynamic_tools

  # Executors (backend choice — already "langgraph" by default)
  single_table_executor: langgraph
  batch_executor: langgraph

  # Optional board chat / AG-UI
  copilotkit_enabled: true

  # Hosted policy codegen (egress)
  allow_external_codegen: true
  codegen_service_url: http://127.0.0.1:8081
  codegen_service_url  # + REDIBIS_CODEGEN_TOKEN env: "${REDIBIS_CODEGEN_TOKEN}"
  codegen_default_target: ranger

  # Bounded in-process execution of approved dynamic tools
  dynamic_sandbox_enabled: true

  # LLM IntentPlanner (omit both lines to keep heuristic-only planning)
  planner_provider: gemini
  planner_model: gemini-2.5-flash

  # Suggest-only by default; keep false unless you accept auto contract writes
  auto_approve_writes: false
  max_retries: 2
  max_plan_repairs: 2

# Durable LangGraph HITL checkpoints (survives API/worker restart)
memory:
  enabled: true
  store: pgvector
  dsn_ref: REDIBIS_MEMORY_DSN
  embedding_provider: sentence_transformers
  embedding_model: all-MiniLM-L6-v2
  domain: telecom
  top_k: 5
  min_similarity: 0.0

# RAI — required for model calls; relax hard_block only if codegen egress is intentional
rai:
  enabled: true
  mode: report
  enforce: false
  block_external_raw_pii: true
  # Must be false when allow_external_codegen: true, or codegen submit is blocked
  hard_block_external_pii: false
  default_residency: local

classification:
  enabled: true
  policy_pack: telecom

contract:
  automerge: none
```

After saving:

```bash
export REDIBIS_CONFIG=/absolute/path/to/redibis-agents-full.yaml
./scripts/webapp.sh --restart
```

### 5. Expected Settings panel after restart

| Setting | Expected |
|---|---|
| Restart required | (label only — still deployment-controlled) |
| enabled | **on** |
| single table executor | langgraph |
| batch executor | langgraph |
| copilotkit enabled | **on** |
| allow external codegen | **on** |
| codegen service configured | **on** |
| dynamic sandbox enabled | **on** |

### 6. Quick verification

Dashboard auth is on by default. Sign in first
([`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl)), then:

```bash
# Master switch + board
curl -s -b "$RB_COOKIES" http://localhost:8000/api/settings | python -m json.tool | head

# CopilotKit mount (when enabled + extra installed)
curl -s -o /dev/null -w "%{http_code}\n" -b "$RB_COOKIES" http://localhost:8000/api/copilotkit/agent

# Codegen status
curl -s -b "$RB_COOKIES" http://localhost:8000/api/agents/codegen/status | python -m json.tool

# Optional planner connectivity
redibis llm test gemini
```

Open **http://localhost:8000/agents** after signing in — Ask / Composer execution should no
longer show the disabled banner. CopilotKit chat appears when the board loads
with `copilotkit_enabled`. Codegen lives on the board **Codegen** path / API.

### What each “all features” switch unlocks

| Switch | Unlocks | Still requires |
|---|---|---|
| `agents.enabled` | Run / plan / execute on `/agents` | Restart + `REDIBIS_CONFIG` |
| `single_table_executor` / `batch_executor: langgraph` | Durable HITL `interrupt()` | `pip install redibis[agents]` |
| `copilotkit_enabled` | `/api/copilotkit/agent` + board chat | `redibis[copilotkit]` + `enabled: true` |
| `allow_external_codegen` + `codegen_service_url` | Hosted Ranger/policy proposal egress | Reachable service + matching provenance secret; RAI must not hard-block |
| `dynamic_sandbox_enabled` | Execute approved dynamic tool source | Tool registered + AST-safe; not a container jail |
| `planner_provider` / `planner_model` | LLM IntentPlanner instead of heuristic | Provider in `llm_providers.json` + API key/endpoint |
| `memory.enabled` + `dsn_ref` | Checkpoints survive process restart | Live Postgres DSN in `REDIBIS_MEMORY_DSN` |

## Optional features one at a time

### CopilotKit / AG-UI only

```bash
pip install -e ".[agents,copilotkit]" -c requirements/constraints.txt
```

```yaml
agents:
  enabled: true
  copilotkit_enabled: true
```

Restart Redibis. The AG-UI endpoint is `/api/copilotkit/agent`. A planner model
is optional; without `agents.planner_provider`, planning uses the deterministic
heuristic planner.

### Hosted code generation only

Follow **[CODEGEN_SERVICE.md](CODEGEN_SERVICE.md)** (runbook §3–4).
Minimal switches:

```yaml
agents:
  enabled: true
  allow_external_codegen: true
  codegen_service_url: http://127.0.0.1:8081
  codegen_service_url  # + REDIBIS_CODEGEN_TOKEN env: "${REDIBIS_CODEGEN_TOKEN}"

rai:
  hard_block_external_pii: false   # otherwise submit is blocked
```

### Dynamic tool execution only

```yaml
agents:
  enabled: true
  dynamic_sandbox_enabled: true
```

Enable this only for reviewed and approved dynamic tools. The sandbox rejects
imports, I/O primitives, reflection, and other forbidden operations, but it runs
inside the Redibis process and is not a replacement for process or container
isolation.

## Troubleshooting

- **The panel still says off:** verify the running process inherited
  `REDIBIS_CONFIG`, edit that exact YAML, and perform a full restart.
- **LangGraph silently falls back:** install `redibis[agents]` in the same Python
  environment used by the web process.
- **CopilotKit stays unavailable:** install `redibis[agents,copilotkit]`, enable
  both `agents.enabled` and `agents.copilotkit_enabled`, and restart.
- **Codegen remains unconfigured:** set `agents.codegen_service_url` in YAML.
  `allow_external_codegen: true` alone is insufficient.
- **Codegen is blocked despite being configured:** check
  `rai.hard_block_external_pii` (must be `false` for egress), service
  reachability, and provenance-secret agreement.
- **HITL resume lost after restart:** enable `memory.enabled` with a working
  `REDIBIS_MEMORY_DSN` (`memory.dsn_ref`).
- **Planner still heuristic:** set `agents.planner_provider` / `planner_model`
  and confirm the provider works via `redibis llm test <provider>`.

For runtime behavior, model-use visibility, and planner precedence, see
[Agentic web experience](AGENTIC_WEB_EXPERIENCE.md). Provider setup:
[LLM providers](LLM_PROVIDERS.md). Codegen:
[CODEGEN_SERVICE.md](CODEGEN_SERVICE.md).
