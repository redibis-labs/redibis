# Agentic web experience

## Routes

- `/` — normal scan console and manual session restoration.
- `/batch` — backward-compatible alias for the scan console.
- `/agents` — Agentic Ask, Composer, Dashboard, and Results (type the URL; not in main nav).
  Operator guide: [`agents/howto/`](agents/howto/README.md). Requires a dashboard
  login ([`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md)).
- `/settings` — canonical settings and effective-runtime view.
- `/v2` — contracts, enrichment, and sharing workspace.
- `/reports` — persisted PII reports.

Every major page links to Scan, Agentic Ask, Contracts, Reports where relevant, and
Settings. The deleted `/review` HTML page is not a valid destination; column-review REST
APIs remain available for integrations.

## Which capabilities use an LLM

| Capability | Model behavior |
|---|---|
| Intent planning | Uses the configured planner provider for unambiguous natural-language requests; otherwise uses the deterministic heuristic planner and reports the fallback. |
| Contract enrichment | Calls the configured enrichment provider only when an `enrich` node executes. |
| Policy code generation | Uses the configured hosted codegen service only for an approved codegen request. Its current post-generation judge is deterministic, not an LLM. |
| Profiling, quality, PII equations, classification | Deterministic unless a separately documented optional model capability is explicitly enabled. |
| Provider connectivity test | A real, bounded model call routed through the same RAI gateway as production calls. |

Agentic Ask shows effective capability configuration before a run. Once a run starts it
shows only model calls associated with that run: purpose, provider, model, planner method,
RAI outcome, and any honest fallback/degraded reason.

## Planner setting precedence

The planner is resolved consistently for Ask, Plan, Composer defaults, and Copilot:

1. `agents.planner_provider` / `agents.planner_model` in `REDIBIS_CONFIG`.
2. `agentic_defaults` saved by `/settings`.
3. The agent run-defaults pack.
4. Deterministic heuristic planning when no provider is configured.

The API reports the winning source. A configured provider does not prove a model was
called; run metadata records actual use.

## Settings and secrets

`/settings` edits hot LLM and planner defaults and displays normalized providers. Runtime
sections show agent execution, RAI, memory, classification, behavior, observability, and
storage state.

Deployment-controlled settings are read-only and marked “Restart required.” Change them
in the YAML selected by `REDIBIS_CONFIG` and restart the web process. The settings API
returns only booleans such as `dsn_configured`, `endpoint_configured`, and
`codegen_service_configured`; it does not return keys, DSNs, provenance secrets, raw
environment values, or the deployment-config path.

Agent execution defaults off. The `/agents` page remains discoverable when disabled, but
execution controls are blocked until an operator sets:

```yaml
agents:
  enabled: true
```

All model completions, including connectivity probes, pass through
`redibis.telemetry.model_gateway.guarded_model_call`.
