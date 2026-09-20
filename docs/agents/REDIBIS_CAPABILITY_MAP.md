# Redibis capability map — planner context & external-code guard

**Audience:** the planning LLM (attach this to its context) **and** the engineers who maintain it.
**Purpose:** tell the agent *what redibis already does natively* so it (a) plans pipelines from
native steps and (b) only writes **external code** when redibis genuinely cannot do the job.

This document has two halves:

1. **Capability surface** — the native tool/node catalogue the planner should map intent onto.
2. **External-code guard** — the policy "generate code only if it's not redibis functionality",
   how it's enforced, and how to maintain it.

---

## 1. Capability surface (what to attach to the planner)

Do **not** hand-maintain a copy of this list. It is generated from the live engine so it never
drifts. Build it at request time and inject it into the planner context:

```python
from redibis.agents.capability_guard import native_capability_surface
from redibis.agents.registry import catalog_for_planner
from redibis.agents.recipes import recipes_for_intent

planner_context = {
    "capabilities": native_capability_surface(),      # id, label, description, phrases, via
    "nodes": catalog_for_planner(),                   # full typed node specs + ports
    "examples": recipes_for_intent(user_intent, k=3), # few-shot golden plans
}
```

- `native_capability_surface()` merges the curated phrase map (`capability_guard.CAPABILITY_KEYWORDS`)
  with the live labels/descriptions from `registry.list_nodes()` and the Tier-2 deep-profile
  catalogue. Each entry is `{id, label, description, phrases, via}`.
- `catalog_for_planner()` is the authoritative node contract (input/output ports, config schema,
  autonomy defaults). The planner must only emit nodes/edges that pass `registry.validate_spec`.
- `recipes_for_intent()` supplies validated few-shot plans (see `docs/agents/` recipes).

### Native capabilities (summary)

| id | What it does natively | Invoke via |
|----|-----------------------|------------|
| `source` | Connect to a local folder, Hive metastore, or Oracle/JDBC and read a bounded sample | pipeline node `source` |
| `sample` | Strategy-based sampling (percent/fixed/statistical/partition) | pipeline node `sample` |
| `profile` | Structural profiling (GE / OpenMetadata / YData): stats, null rates, distributions, cardinality | pipeline node `profile` |
| `pii` | PII detection (Presidio regex + GLiNER NER), equation-engine verdicts | contract sub-flag `pii` |
| `quality` | Data-quality rule generation (Great Expectations expectations) | contract sub-flag `quality` |
| `classify` | Multi-tag classification from the policy/classification pack, jurisdiction-aware | contract sub-flag `classify` |
| `mask` | Masking-strategy recommendation per PII column (mask/hash/encrypt/FPE/fake) — enforced externally | pipeline node `mask` / contract `mask_rules` |
| `contract` | Build/merge an ODCS v3 data contract (full schema, not one column) | pipeline node `contract` |
| `publish` | Publish the contract to OpenMetadata (Atlas planned) | pipeline node `publish` |
| `gate` | Human approval gate (steward/owner) before irreversible/external actions | pipeline node `gate` |

> The table is a human aid. The machine-readable, always-current version is
> `native_capability_surface()` + `catalog_for_planner()`. If they disagree, the code wins.

---

## 2. External-code guard — "only if redibis can't do it"

### Policy

When the user asks the agent to **generate code** (a Ranger policy, a masking script, a quality
check, a profiling routine, etc.), redibis must **prefer the native pipeline** and generate
external code **only when no native capability covers the request**. Native paths are governed,
audited, contract-aware, and reversible; generated code is *proposed-not-executed* and carries
egress + vuln-scan + LLM-judge + human-approval overhead. Redundant code is a liability.

### Where it's enforced

`redibis/agents/capability_guard.py` is the single seam:

- `match_native(intent)` → keyword-matches the intent against `CAPABILITY_KEYWORDS` (enriched with
  live registry labels). Returns the overlapping `NativeCapability` records.
- `decide_codegen(intent, force_external=False)` → the policy decision:
  - native overlap **and not** forced → `generate=False` (steer to the pipeline);
  - native overlap **and** forced → `generate=True` (recorded in the audit reason);
  - no overlap → `generate=True`.

The codegen route applies it **before** any generation:

```
POST /api/agents/codegen/submit   (redibis/webapp/backend.py)
  → decide_codegen(intent, force_external)
      ├─ generate=False → returns {"status": "native_capability_available", "decision": {...}}
      └─ generate=True  → submit_codegen(...) → egress validation → backend → proposed code
```

The composer renders `native_capability_available` as a "redibis can already do this" card listing
the matched capabilities, with a **"Generate code anyway"** button that re-submits with
`force_external: true`.

### Layers of defence (in order)

1. **Composer routing** — `isCodegenIntent()` only routes obvious code asks to codegen; everything
   else builds a native pipeline. (Heuristic, frontend.)
2. **`decide_codegen` guard** — the backend gate above. Heuristic, always on, safe-by-default.
3. **Commercial LLM judge** (T8.2) — can make a *semantic* "is this native?" call and override the
   heuristic. The heuristic remains the floor when no judge is configured.
4. **Egress + RAI** — even when generation is allowed, `prepare_codegen_request` + `hard_block`
   enforce data-residency/PII egress rules independently.

---

## 3. How to maintain this

**Adding / changing native coverage (the common case):**

- Edit `CAPABILITY_KEYWORDS` in `redibis/agents/capability_guard.py`. Add the capability `id`
  (ideally matching a registry node `type` or Tier-2 capability id) and the **natural-language
  phrases** users actually type. That's the whole change — the guard and the exported surface pick
  it up automatically.
- If you add a brand-new engine capability, register the node in `redibis/agents/registry.py`
  (so `catalog_for_planner()` exposes it) and/or a Tier-2 capability in `deep_profile`, then add its
  phrases to `CAPABILITY_KEYWORDS`. Labels/descriptions flow in from the registry — don't duplicate
  them in the keyword map.

**Tuning the guard:**

- Too aggressive (blocking legitimate code)? Narrow the phrases, or rely on `force_external`.
- Too lax (allowing redundant code)? Add the missing phrases, or wire the commercial LLM judge.

**Keeping the planner doc honest:**

- Never paste a static capability list into the planner prompt. Always call
  `native_capability_surface()` + `catalog_for_planner()` at request time.
- Add a test that asserts every `CAPABILITY_KEYWORDS` id resolves to a registry node or Tier-2
  capability (catches drift when a node is renamed/removed).

**Tests to keep green:**

- `decide_codegen("mask the email column")` → `generate=False`, match includes `mask`.
- `decide_codegen("scrape an external REST API and load it")` → `generate=True` (no native match).
- `decide_codegen("profile the table", force_external=True)` → `generate=True`, match includes
  `profile` (override recorded).

---

## 4. Invariants this supports

- The agent calls the **typed tool surface + Tier-2 catalogue**, never raw library imports
  (AGENTS.md invariant 8).
- Generated code is **proposed-not-executed**: judged + scanned + approved + sandboxed
  (AGENTS.md invariant 9).
- Suggest-only is the default for LLM-derived decisions; writes/external/irreversible actions are
  gated (AGENTS.md invariant 10).
