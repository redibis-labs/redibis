# Evidence Review Ledger

Operator guide for the run-centric Evidence Review Explorer, steward-driven
review/decisions, portable verdict export/preview/import, and CLI replay.

## Concepts

| Term | Meaning |
|------|---------|
| **Run** | One completed scan's evidence (`evidence_bundle.json` + `evidence_manifest.json`), addressed by `run_id` |
| **Engine proposal** | Fresh detector + equation output from a scan (`pii_verdict` in `evidence_bundle.json`) |
| **Effective verdict** | What downstream review and reports show after authority resolution |
| **Steward decision** | Durable overlay in `_meta/pii_decisions/{table}.json`, with an append-only `history` |
| **Fingerprint** | `name_normalized \| logical_type \| format_signature` — drift baseline per column |
| **Stale** | Fingerprint changed; prior steward decision retained for audit but no longer authoritative |
| **Verdict package** | Portable `redibis.verdict_package` JSON/JSONL — decision identity, fingerprint, lifecycle, no raw values |
| **Import audit log** | Append-only `_meta/verdict_imports/{table}.json` record of every durable verdict promotion |

Precedence (highest first):

1. Active local steward decision (matching fingerprint)
2. Matching run-scoped supplied verdict (`--verdicts` on CLI rescan, or `.../verdicts/replay`)
3. Engine proposal

A stale or mismatched decision/verdict is always **visible** (drift reasons
shown in the explorer) but never silently authoritative — it falls back to
the engine proposal until a steward re-confirms.

## Run explorer — single-table review

1. Run a scan (UI or CLI).
2. Open **Evidence review** from the PII page, Contracts v2 **Evidence
   Review** tab, or visit `/review?table=db.table[&run_id=...]` directly.
3. Pick a run from the run selector (newest first) and walk the phase tabs:
   **Overview** (searchable column × effective/engine/coverage/drift matrix),
   **Profiling**, **Quality**, **PII** (per-engine drawers — regex, NER +
   entities, phone/NID/IMEI/IMSI/geo validators, custom rules, LLM/multistep
   LLM — with score, hits, thresholds, rule/equation trace, timings, and
   errors), **LLM** (guarded model calls, parent/child steps, tokens,
   latency, cost, validation status), **Artifacts** (every manifest entry,
   inline JSON/text/HTML viewers or download), and **Decisions**.
4. On **Decisions**, set PII / not-PII, correct the entity type, add a
   reviewer + reason, and see prior decisions under **History**. Every
   decision captures the run's evidence digest and column fingerprint —
   there is no un-fingerprinted "legacy" authority.
5. Exact restricted evidence (raw LLM prompts/responses) stays hidden by
   default; the LLM tab's **View exact evidence** action asks for an actor +
   reason, enforces `evidence.access` policy, and appends an audit row
   before returning the raw payload (see `docs/EVIDENCE_STORE.md`).

Two runs can be diffed without mutating either: `GET
/api/evidence/{table}/runs/compare?a=<run>&b=<run>` reports per-column
`present_in_a/b` and `changed` for engine proposal, effective verdict, and
coverage.

## Batch review

Enterprise **Reports** ingests `evidence_bundle.json` when present in scan
output folders. Table detail panels embed the same `EvidenceReviewer`
component (`redibis/webapp/static/evidence_reviewer.js`) used by `/review`
and the v2 tab — one explorer, every surface.

## Drift matrix

| Change | Effect on column decision |
|--------|---------------------------|
| New column | Unreviewed; no prior decision |
| Removed column | Decision retained in overlay (audit); column absent from contract |
| Rename | Treated as drift unless lineage mapping exists |
| Logical / physical type change | `stale` |
| Format signature change | `stale` |
| Unrelated column added elsewhere | Existing decisions stay `active` |

Stale decisions are **not deleted** — they remain in the overlay for audit but `reconcile_pii_columns` skips them until a steward re-confirms.

## Portable verdict export

```bash
# All tables with steward decisions
redibis verdict export --out verdicts.json

# One table
redibis verdict export --table telecom.customers --out verdicts.jsonl --jsonl
```

REST:

- `GET /api/verdicts/export` — all tables
- `GET /api/contracts/{table}/verdicts/export` — one table

Schema: `redibis.verdict_package` v1.0 — table/column identity, fingerprint, status (`pii` \| `not_pii`), lifecycle, actor, reason, timestamps. **No raw values or samples.**

Load into an external governance database by mapping `entries[]` to your
verdict table, or bring it back into Redibis with **preview → import**
below — Redibis overlays remain the local authority either way.

## Verdict preview / import (environment A → environment B)

A verdict package exported from one environment (or an earlier run) is
never trusted blindly on another table's current evidence. Every entry is
classified **before** any write:

| Category | Meaning |
|---|---|
| `matching` | Fingerprint-clean, no local conflict — safe to promote |
| `stale` | Fingerprint mismatch vs. this run's column — schema/format drifted |
| `missing` | Column absent from this run's evidence bundle |
| `conflicting` | An **active** local steward decision already disagrees |
| `invalid` | Malformed entry — no column, no fingerprint, or wrong table |

```bash
# Never writes — classify only
redibis verdict preview telecom.customers --package prior.json --run-id <run>

# Durable promotion: requires an actor + reason; records one audit event
redibis verdict import telecom.customers --package prior.json \
  --run-id <run> --actor alice --reason "reconciled from staging" \
  --merge-policy matching_only        # or overwrite_conflicts
```

`--merge-policy overwrite_conflicts` also promotes `conflicting` entries
(the steward's explicit override); `stale`, `missing`, and `invalid` entries
are **never** promoted regardless of policy. Import writes through
`PiiDecisionStore` / `ContractStore.set_pii_decision` — no new contract-bucket
writer — and appends one record to `_meta/verdict_imports/{table}.json`.

REST equivalents (same classification/promotion rules), used by the
explorer's **Decisions** tab verdict panel:

- `POST /api/evidence/{table}/verdicts/preview` — `{package, run_id?}` → classification, never writes
- `POST /api/evidence/{table}/verdicts/import` — `{package, run_id?, actor, reason, merge_policy?}` → promotes + audits
- `GET /api/evidence/{table}/verdicts/imports` — the import audit log
- `POST /api/evidence/{table}/runs/{run_id}/verdicts/replay` — run-scoped, non-mutating dry run (see below)

## CLI rescan with prior verdicts

Replay is **run-scoped** — it does not mutate `PiiDecisionStore`, contracts, or column memory.

```bash
# Export steward decisions from a prior environment
redibis verdict export --table telecom.customers --out prior.json

# Replay at new thresholds + overlay matching historical decisions
redibis scan decide --table telecom.customers --preset reporting \
  --verdicts prior.json --out replayed.json
```

Steward Review A1 (`redibis steward export-verdicts` / Finalize artifact
`steward_verdicts.json`) is accepted as `--verdicts` as well — PII rows are
projected to this package. Tutorial:
[`docs/tutorials/STEWARD_REVIEW_ARTIFACTS.md`](tutorials/STEWARD_REVIEW_ARTIFACTS.md).

To **write** those human-verified columns into the overlay on the next
`scan` / `enrich` / `deep-scan` (and skip det/LLM for them), use
`--steward-verdict-path` — walkthrough:
[`docs/tutorials/STEWARD_VERDICT_ATTACH.md`](tutorials/STEWARD_VERDICT_ATTACH.md).

Warnings:

- `stale fingerprint (ignored)` — supplied entry fingerprint does not match current column
- `no matching verdict` — column had no usable supplied entry

Matching supplied decisions appear in output under `steward_review` with
`effective_verdict.source = supplied`. The REST equivalent of this dry run —
"what would apply if I promoted this package into *this* run" — is `POST
/api/evidence/{table}/runs/{run_id}/verdicts/replay`; it returns `applied`
(columns whose supplied verdict became effective), `stale_skipped`,
`missing_skipped`, `conflicting`, and `invalid_skipped`, and writes nothing.

## API surfaces

| Route | Purpose |
|-------|---------|
| `POST /api/evidence/review` | Normalize review from an evidence bundle JSON body |
| `GET /api/evidence/{table}/review` | Load latest run bundle + overlays for a table (side-effect free) |
| `GET /api/evidence/{table}/runs` | List every run id with evidence for a table |
| `GET /api/evidence/{table}/runs/{run_id}` | Full run overview (all phases, columns, artifacts) |
| `GET /api/evidence/{table}/runs/{run_id}/columns/{column}` | One column's drill-down for a run |
| `GET /api/evidence/{table}/runs/{run_id}/artifacts` | Manifest artifact index for a run |
| `GET /api/evidence/{table}/runs/{run_id}/artifacts/{name}` | Fetch one shareable artifact (403 if restricted) |
| `GET /api/evidence/{table}/runs/compare?a=&b=` | Non-mutating two-run diff |
| `POST /api/evidence/{table}/restricted/llm` | Audited exact-evidence read (`actor`/`reason`/`role` required) |
| `POST /api/evidence/{table}/review/recompute-drift` | Explicit drift evaluation + persistence (never on GET) |
| `GET /api/contracts/{table}/review/columns/{column}/history` | Full decision history for one column |
| `POST /api/evidence/{table}/verdicts/preview` \| `.../import` | See verdict preview/import above |
| `GET /api/evidence/{table}/verdicts/imports` | Import audit log |
| `POST /api/evidence/{table}/runs/{run_id}/verdicts/replay` | Run-scoped, non-mutating replay dry run |
| `GET/POST/PUT /api/contracts/{table}/review/*` | Final Review checkpoint (existing) |

## Architecture notes

- **Authority**: `PiiDecisionStore` + `ReviewStore` overlays; `ContractStore.upsert()` reconciles active decisions and evaluates/persists drift on every write (`evaluate_and_mark_drift`) — never on a GET.
- **Evidence**: `evidence_bundle.json` is immutable run evidence; never overwritten by steward actions.
- **Fingerprint by default**: `ContractStore.set_pii_decision()` auto-derives fingerprint metadata from the active contract when the caller omits it — there is no path to an un-fingerprinted decision.
- **History**: every overwrite of a `PiiDecision` appends the prior snapshot to `history` (capped, newest first); `PiiDecisionStore.history()` returns the full audit trail.
- **Import audit**: `VerdictImportLog` (`_meta/verdict_imports/{table}.json`) is append-only and records actor, reason, merge policy, and applied/skipped columns for every `verdict import`.
- **Memory**: pgvector column memory remains **suggest-only** — retrieved past decisions never auto-apply.

See also: `docs/EVIDENCE_STORE.md`, `docs/internal/FINAL_REVIEW_DESIGN.md`.
