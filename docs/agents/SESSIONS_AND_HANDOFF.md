# Agent runs vs manual sessions — identifiers, file structure, and handoff

This explains how the **agent** context and the **manual 360°** context stay separate (so a
background batch and a manual session can run at the same time without clobbering each other),
and how a single table is handed off from the agent dashboard to the manual console.

## Two independent namespaces

| Context | Identifier | Stored in | Scope |
|---|---|---|---|
| Agent batch run | `agent_run_id` (`AgentRun.run_id`) | `LineageStore` (`_agent_lineage_store`) — `runs/<run_id>/run.json` | **many tables**, each a job with its own per-table scan `run_id` |
| Per-table scan | scan `run_id` (in `StepRecord.output["run_id"]`) | `report.output_dir/<run_id>/` (sample, artifacts, contracts) | one table |
| Manual session | `session_id` | `SessionManager` (`session_manager._sessions`) + `scan_output/<session_id>/` | **one active table at a time** |

These are **different stores keyed by different ids**. The agent dashboard tracks the
`agent_run_id` (frontend: `agents.js` `state.runId`); the manual console tracks `session_id`
(frontend: `app.js` `S.sid`). They are separate pages with separate state, so a background
agent batch and a manual session coexist without sharing a global.

> **`AgentRun.session_id` is NOT a manual session.** The LangGraph executor attaches an
> `AgenticSession` (a manifest, stored in the lineage store via `save_manifest`) and sets
> `AgentRun.session_id` to *that* id. The executor never writes into `session_manager`, so a
> background agent run cannot clobber a manual session. A real manual `ScanSession` is created
> only at handoff. (This is why the live agent log streams from
> `GET /api/agents/runs/{run_id}/stream`, not `/api/sessions/{id}/stream`.)

> Think of `agent_run_id` as the agent's "session id". A batch run fans out to many per-table
> scan `run_id`s; the manual console only ever holds one of them at a time.

## File structure (mirrors manual redibis)

Each per-table scan writes under `report.output_dir/<run_id>/`, the same layout a manual scan
produces under `scan_output/<session_id>/`:

```
<run_id>/
  sample.csv                 # the scanned sample (persisted by the agent scan steps)
  quality_contract.yaml
  pii_contract.yaml
  interactive_review.html    # quality report
  pii_regex_review.html      # PII report
  ge_report/index.html       # GE data docs
```

On handoff, `materialize_session_folder` copies `sample.csv → data.csv` and links the
artifacts into `scan_output/<session_id>/`, so the manual page sees a normal session.

## Handoff: agent table → manual session

`POST /api/agents/handoff {run_id, table}` →

1. `_run_id_for_table` resolves the chosen table's scan `run_id` from the agent run.
2. `materialize_session_folder` writes `scan_output/<run_id>/session.json` (using the scan
   `run_id` as the manual `session_id`) and copies the data + artifacts.
3. `rehydrate_scan_session` loads it into `session_manager`; the response `redirect_url`
   (`/?session=<session_id>`) opens the manual console.

The manual `session_id` **is** the table's scan `run_id` — that's the "assign the agent runid
to redibis" the design calls for. Picking a different table hands off a different `run_id` to a
different manual session; the manual console still only shows one at a time.

## The crash that this fixes

Previously the agent scan loaded each table's sample into memory (`state.df`) but **never wrote
it to disk**. Handoff then built a session with no `data.csv`, so the manual console's
`/data/columns` and masking/preview endpoints read a missing file and the session "crashed"
(opened empty / 500). Fixes:

- `tool_runner._persist_run_sample` writes `state.df → <output_dir>/<run_id>/sample.csv` in the
  profile / PII / quality steps (mirrors the manual `data.csv`).
- `handoff_to_manual_session` now fails with a clear message ("no sample data persisted for
  <table>…") instead of producing a broken session when a table wasn't scanned.

## Maintenance notes

- **Don't collapse the two namespaces.** Keep agent runs in the `LineageStore` and manual
  sessions in `session_manager`. They share the on-disk run folder (`report.output_dir`) by the
  scan `run_id`, which is the bridge — not a shared in-memory global.
- **`report.output_dir` must equal the scan `ScanConfig.output_dir`** (it does today:
  `to_scan_config` falls back to `cfg.report.output_dir`). If you ever pass a custom
  `output_dir` to `to_scan_config` in the agent path, update `materialize_session_folder` to
  look there too, or handoff won't find the sample/artifacts.
- **Adding a new report artifact?** Write it into `<run_id>/` and add its filename to
  `handoff._artifact_paths` so it links into the manual session.
- A new manual session id (instead of reusing the scan `run_id`) would only be needed if two
  tables ever shared a `run_id` — they don't (each table gets its own).
