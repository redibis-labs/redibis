# 03 — Dashboard & Results (progress, HITL, handoff)

**Goal:** monitor batch runs, approve gates, open artifacts, and hand a table to manual Scan.  
**Time:** 60 minutes.  
**Tabs:** **Dashboard**, **Results**

---

## 1. Dashboard

Shows **multi-table** progress for the active agent `run_id`:

- Status chips per table (`pending` / `running` / `completed` / `awaiting_hitl` / `failed`)
- Counts (done / total)
- **Open** → jumps to Results focused on that table
- Run picker when several runs exist under `runs_dir`

Use Dashboard when Ask/Composer started a batch (more than one table).

---

## 2. Results

Per-run detail:

| Feature | Use |
|---------|-----|
| Table grid | Select a table node |
| Summary card | Status, step list, errors |
| HITL banner | Approve / reject when paused at a gate |
| Artifacts | Download / open reports produced so far |
| Live log | Stream from `/api/agents/runs/{id}/stream` |
| **Handoff** | Materialize a manual Scan session for that table |

---

## 3. Human-in-the-loop (HITL)

When `auto_approve_writes` is **false** (recommended for shared hosts / demos of stewardship):

1. Pipeline hits a `gate` (or gated write).
2. Run status becomes `awaiting_hitl`.
3. Results shows the interrupt payload (role, table, node).
4. Steward clicks **Approve** or **Reject** (API: `POST /api/agents/runs/{id}/resume`).
5. Batch continues to the next table if applicable.

With default **auto_approve_writes: true**, gates are skipped — fine for local labs, not for
steward training. Set false in YAML for HITL labs (see [07](07-safety-and-settings.md)).

---

## 4. Handoff to manual Scan

Agent runs and manual sessions use **different stores**. Handoff copies the table’s scan
artifacts into a Scan console session:

1. Select the table on Results.
2. Click **Handoff** (optional sample path).
3. Follow `redirect_url` to `/?session=…` on the Scan console.

Details: [`SESSIONS_AND_HANDOFF.md`](../SESSIONS_AND_HANDOFF.md).

After handoff, stewards use normal approved-basket / contract merge flows on `/`.

---

## 5. Lab exercise

1. Run a two-table batch with `auto_approve_writes: false` and a `gate` in the graph.
2. On Dashboard, wait until one table is `awaiting_hitl`.
3. Approve on Results; confirm the second table pauses or completes as designed.
4. Handoff one completed table; open Scan and find the sample / reports.

**Pass criteria:** learner can resume HITL and explain that handoff creates a *manual*
session, not the agent lineage id.

---

## Next

→ [04 — Review & audit](04-review-and-audit.md)
