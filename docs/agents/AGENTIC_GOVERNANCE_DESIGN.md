# redibis — Agentic Governance & Classification: Consolidated Design

**Status:** design + handover. Audience: an engineer/LLM with no prior context.
**Read first:** repo `CLAUDE.md` (invariants), `docs/unified_config/UI_SAFETY_CHECKLIST.md`,
`docs/memory/HANDOVER_PLAN_column_memory.md` (the memory/fingerprint layer this builds on).

This document consolidates a long design discussion into one place: the two
operating paths, the full agentic feature set, the data-classification policy
engine (grounded in the telecom Apache Atlas guide), the masking model, the
cross-cutting telemetry/RAI layer, and the open-core vs. commercial boundary.

---

## 0. The one principle that governs everything

redibis is a **deterministic contract / metadata / recommendation brain.** It
does not transform or move data at runtime and it does not enforce policy —
**enforcement and transformation live in external systems** (Apache Ranger,
Trino, the source DBs). Every new capability — manual UI, agentic board, batch
orchestration — is a **thin client over the same deterministic core**, never a
second implementation of it. The moment an agent or a board contains domain
logic (a threshold, a co-tag rule, a contract shape), that is a bug; it belongs
in the core where every client benefits.

LLMs are used **only at judgment nodes** (ambiguous classification, definitions,
PII adjudication). The spine — sample, profile, scan, classify-rules, emit
contract — stays deterministic and reproducible, because contracts are
compliance artifacts.

---

## 1. Two paths, one core

| Path | What it is | Status |
|---|---|---|
| **Manual** | add sample → scan → review/adjust every column → enrich → review → save contract; the table/column data + steward decisions are captured for the learning loop | mostly built (scan core + memory layer) |
| **Agentic** | a drag-and-drop board that **compiles to an editable, tuned prompt-plan**; the plan orchestrates the *same deterministic tools*; optionally runs in batch across a whole DB | to build |

Both call the same framework-neutral tool layer (the one scoped for the future
ADK adapter): `Scan`, classification engine, `EnrichmentService` + `ContextRetriever`,
catalog publishers, `MetadataRetriever`. The board does not *do* governance — it
*composes* it.

### The board → prompt-plan model (v1)
- Drag an icon → a config panel (its params). A live side-panel renders a
  **sectioned, editable prompt-plan**: Goal → Source → ordered Steps (with params)
  → locked Guardrails → Output format → approval gates.
- The prompt is a **plan that references deterministic tools**, not free-form
  instructions the LLM acts out. Each node = a config panel **and** a bound tool
  call.
- redibis invariants render as **locked, non-editable guardrail clauses**.
- One source of truth per node (renders both the param form and its prompt
  fragment) so graph and prose never drift; graph edits regenerate with a diff so
  manual prompt edits aren't silently lost.
- **v1 = export-only** (compose → tune → hand the plan to an agent/LLM). **v2**
  binds the same nodes to tools and executes in-app. Starting export-only proves
  the UX with zero execution wiring; the structured graph behind the prompt is
  reused unchanged in v2.
- UI library: **React Flow** for the canvas; palette rendered from a node
  registry (so new tools appear automatically).

---

## 2. Agentic feature catalog (the 11)

| # | Feature | Lands on | Open/Commercial |
|---|---|---|---|
| 1 | Collect external data + metadata + sampling strategy/tier; choices guided by prompt **or** global config | `MetadataRetriever` registry + 3-tier profiling + `SamplingConfig`; a logged "decision" step when config is open-ended | Open |
| 2 | Batch the whole pipeline across all tables | `LoopAgent` fan-out over `MetadataRetriever` table list | Open core; at-scale scheduling = commercial |
| 3 | All scan types (profile / quality / PII / classify) | existing `Scan` phases + classification engine | Open |
| 4a | Generate a task list + reconcile **planned vs. accomplished** | planner emits a task ledger; end-of-run reconciliation | Open |
| 4b | Visualize the batch job; open any completed table's reports/contract | batch dashboard + run store | Open |
| 5 | **Cancel a job anytime** | cooperative cancellation tokens checked between steps; clean resumable boundaries (never mid-write) | Open |
| 6 | Generate batch jobs / code → LLM-as-judge → vuln scan → human view/approve → run → package as external API → **agents register dynamically** | codegen safety chain + dynamic tool registry (MCP-like) | **Commercial** |
| 7 | Auto-document a generated feature/function: what, how, benefits, risks, how to monitor | doc-gen tool (read-only output) | Open (growth lever) |
| 8 | Step-by-step **DAG view of what agents actually did** (explainable, auditable) | run/lineage store → React Flow trace render | Open core; compliance reporting commercial |
| 9 | **OpenTelemetry** across the agent layer | span per agent step / tool call / model call (tokens, latency, cost, model id, local/public) | Open |
| 10 | **Responsible-AI layer** per run and per model call | guardrail middleware: PII-in-prompt checks, groundedness, decision logging, model provenance, policy allow/deny | Open core; managed RAI dashboards commercial |
| 11 | **Multi-classification** (not just PII/GLiNER) → Apache Atlas | classification policy engine (§3) | Engine + packs **open**; enforcement-translation commercial |

Notes that shape several of these:
- **Codegen (6)** is the riskiest: it deliberately opens a non-deterministic door
  in a deterministic product. Acceptable only if generated code is always
  *proposed-not-executed*, sandboxed, SAST/vuln-scanned, LLM-judged, human-approved,
  and unable to bypass invariants. "Register dynamically" = a runtime tool
  registry (the MCP pattern); approved+packaged code becomes a callable tool. Use
  **Monaco** (MIT editor) for review, or **code-server/openvscode-server** for a
  full in-browser IDE.
- **OTel (9)** also resolves the global-root-logger concurrency hazard flagged in
  the post-implementation review — context propagation isolates per-run telemetry
  properly.

---

## 3. Data classification — the policy engine (feature 11)

Grounded in the telecom **Apache Atlas classification guide**. The key realization:
**classification is ~90% a deterministic rules engine and ~10% an LLM.** GLiNER/
Presidio is one evidence source for one domain (PII); the system is multi-domain,
multi-label, and rule-governed.

### 3.1 Taxonomy-as-config: the `ClassificationPolicy` pack
Open, editable, governed, **versioned**. A declarative pack (YAML/JSON) encoding:

- The **8 domains**: DataSensitivity, DataSecurity, RegulatoryCompliance,
  TelcoDataType, DataQuality, Lifecycle, RetentionPolicy, PrivacyState.
- Every **tag** + its required/optional **attributes** (e.g. `biometric_type`,
  `cdr_type`, `retention_period_days`, `transfer_mechanism`).
- The **golden rules** as machine-checkable constraints:
  exactly ONE DataSecurity; any DataSensitivity → DataSecurity≥Confidential;
  co-tags atomic in one API call; LawfulIntercept = SecOps-only (agent
  detect-and-escalate, never apply); Pseudonymized ≠ Anonymized; Archived/Churned →
  RetentionPolicy.
- The **co-tag matrix** (the cheat sheet) and the **DataSecurity decision shortcut**.
- The **retention table** (longest-wins) + column-vs-table applicability.
- The **role-routing matrix** = the guide's "Who Verifies" column (Steward /
  Architecture-auto / SecOps / DPO / Legal).

Telecom is one pack; finance/health are other packs. **Industry policy packs are
open** (governed, versioned); the commercial value is elsewhere (§5).

### 3.2 Classifier ensemble (evidence producers → candidate tags + confidence)
- PII/entity → Presidio + GLiNER (existing) → DataSensitivity:PII, SubscriberPII/Biometric hints.
- Format/regex catalog → MSISDN, IMEI, Aadhaar, card, lat/long → TelcoDataType + sensitivity.
- Column/table name + business-glossary lookup.
- **LLM (judgment node only):** the ambiguous mapping — SubscriberPII vs CDR_Data,
  BusinessDomain, CPNI category — fed the **redacted column card + retrieved past
  steward classifications** (the memory loop). Suggest-only.

### 3.3 The deterministic policy engine (the heart)
Inputs: candidate tags + table/column context. It then, deterministically:
applies golden rules → expands mandatory co-tags → derives DataSecurity → computes
retention (longest applicable, reconciled to the contract's single retention block)
→ fills/`flags` required attributes → hard-stops LawfulIntercept → validates against
the pre-production checklist. **LLM proposes; the engine disposes.** Fully testable.

### 3.4 Jurisdiction comes from the data steward
Regulatory tags (GDPR / TRAI / CPNI / …) depend on **data-subject geography**,
which values cannot reveal. The **steward supplies the jurisdiction signal**
(per-table/domain), the engine derives the regulatory co-tags from it. This is the
one part automation cannot close alone.

### 3.5 Atlas publisher (extend the existing one)
Emit **all co-tags in ONE POST** with attributes (atomicity = golden rule 3),
`propagate=false` for LawfulIntercept, then **verify by read-back**. The guide's
worked examples A/B/C and the co-tag cheat-sheet/checklist are the **test fixtures**.

### 3.6 PrivacyState reflects reality, not intent
A column whose contract says "FPE for analytics" is still `RawPersonalData` until
the external system actually tokenizes it. The **contract carries the intended
masking rule; `PrivacyState` carries the actual transformed state.** Do not tag
`Pseudonymized` prematurely. (Optional later refinement: verify the pseudonymized
view exists before promoting the tag.)

---

## 4. Masking model — declarative annotation, external enforcement

**redibis does not mask data for the analytics path.** Feature 2 = **mark the
column in the contract** (e.g. "FPE for the analytics role") in the per-column
`privacy`/`maskingPolicy` block (already supported by `contracts/masking_policy.py`
+ `privacy.py`). Enforcement is external — **Apache Ranger / Trino** apply the
masking at query time per role. The **commercial agent reads the rule from the
contract and generates the Ranger policy** (this is the feature-6 codegen path:
judged, vuln-scanned, approved, applied).

Consequences:
- The per-run-key vs. stable-analytics-key tension **leaves redibis** entirely —
  key management belongs to Ranger/Trino.
- **Role/strategy catalog → policy pack** (same open, governed, versioned pack as
  the taxonomy).
- redibis's **masking engine** (real FPE/HMAC transform, per-run keys) now only
  serves the "produce a one-off safe de-identified export" use case. **Decision
  to confirm:** keep it as a feature or deprioritize, since the analytics path is
  external.

---

## 5. LLM residency & the memory store — keep these separate

- **Inference (transient):** with a **local/on-prem LLM**, passing **raw sample
  rows is fine** — nothing leaves the network. Masking-for-LLM is therefore
  **destination-conditional**: local → raw allowed; external/public → mask-or-block
  (enforced by the RAI layer, §6).
- **Persistence (durable):** the **memory store / `column_card` stays
  format-signature-only regardless of LLM locality.** "Raw to a local LLM" must
  never become "store raw values." This is the compliance fix from the
  post-implementation review and it stands unconditionally — inference ≠ persistence.

The memory loop (retrieved past steward decisions as suggest-only context) feeds
**both** enrichment **and** classification — it is the same RAG-over-reviews
mechanism, now powering the multi-tag judgment node.

---

## 6. Cross-cutting: OpenTelemetry + Responsible AI

- **OTel:** a span per agent step, per tool call, and per model call (tokens,
  latency, cost, model id, local/public, run id). This is the backbone of the
  feature-8 DAG trace and the audit story, and it fixes per-run log isolation.
- **RAI middleware (around every model call):** block raw PII reaching an external
  model (residency policy); groundedness/hallucination checks for enrichment;
  log prompt + output + decision + provenance; model allow/deny by policy. Enforced
  in code (ADK-style callbacks), never in prompts. Per-run and per-call records
  become part of the lineage store.

---

## 7. Open-core vs. commercial boundary

| Open (adoption / standard) | Commercial (enterprise moat) |
|---|---|
| Core contract engine, scan/profile SDK, ODCS format | Enforcement-translation codegen (contract → **Ranger/Trino** policy) |
| Classification engine **+ industry policy packs** | Dynamic-tool / codegen platform (feature 6) + its safety chain at scale |
| OSS-catalog publishers (OpenMetadata / Atlas / DataHub) | At-scale agent orchestration + control plane (RBAC/SSO, audit/compliance reporting, scheduling, drift) |
| Single-pipeline board → prompt-plan composer | Enterprise/proprietary connectors (Collibra, Alation, Informatica, ServiceNow, IAM) + certification + support |
| Memory loop, OTel/RAI hooks | Managed RAI dashboards; hosted/managed service + SLA |
| **Auto-docs generation** (recommended open — top-of-funnel) | — |

Licensing notes (take to counsel — not legal advice): consider **Apache-2.0 over
MIT** for the open core (adds a patent grant); commercial modules either fully
proprietary (separate package, license-gated via the registry seam) or a
source-available license; adopt a **CLA/DCO** before taking community PRs into the
open core. The plugin/registry architecture is the clean open/commercial seam —
commercial features are registered plugins in a proprietary package.

---

## 8. Building blocks still missing (in dependency order)

1. **`ClassificationPolicy` pack schema** (taxonomy, attributes, golden rules, co-tag
   matrix, retention table, role-routing) — open, versioned.
2. **Classification policy engine** (deterministic rule resolution + co-tag expansion
   + retention + validation) and a **classifier-ensemble adapter** feeding it.
3. **Atlas publisher extension** (atomic co-tags, attributes, `propagate=false`,
   verify read-back).
4. **Jurisdiction/context capture** (steward input surface feeding regulatory tags).
5. **Role-routed approval/gate model** (the "Who Verifies" matrix).
6. **Memory retrieval into classification** (extend `ContextRetriever` usage).
7. **PipelineSpec / node registry / prompt-plan compiler** (the board's output).
8. **Batch executor + cooperative cancellation**.
9. **Run/lineage store + DAG trace view** (React Flow).
10. **OTel instrumentation + RAI middleware**.
11. **(Commercial) codegen safety chain + dynamic tool registry**; **contract→Ranger
    translator**; control plane.

---

## 9. Phased rollout

```
Phase 1  Classification policy pack + engine + Atlas publisher   ← deterministic, huge value, NO agents
Phase 2  Memory-into-classification + role-routed approval gates
Phase 3  Board → editable prompt-plan (EXPORT-ONLY)
Phase 4  Batch orchestration + cancellation + DAG trace + OTel + RAI
Phase 5  In-app execution (bind board nodes to deterministic tools)   ← becomes the agentic path
Phase 6  (Commercial) codegen → Ranger, dynamic tools, control plane, enterprise connectors
```

Phase 1 ships real value (governed multi-tag classification → Atlas) before a
single agent exists. Agents are layered on once the deterministic spine + lineage
+ RAI are in place.

---

## 10. Invariants & guardrails (must hold throughout)

1. `ContractStore.upsert()` is the only contract-bucket writer.
2. Detector produces evidence; equation produces verdicts; the classification
   engine is deterministic — the LLM only *proposes* at judgment nodes.
3. The memory store / column card is **format-signature-only**, regardless of LLM
   locality (inference ≠ persistence).
4. `PrivacyState` = actual transformed state; the contract carries intent.
5. LawfulIntercept: agents **detect and escalate, never apply**.
6. Masking enforcement is external (Ranger/Trino); redibis only annotates the
   contract.
7. Generated code is proposed-not-executed: judged + scanned + approved + sandboxed;
   it cannot bypass 1–6.
8. Writes and irreversible/external actions are policy-gated; suggest-only is the
   default for LLM-derived decisions; a human (the routed role) approves.
9. Every model call passes the RAI middleware and emits an OTel span.
10. New engines/classifiers/publishers/connectors are **registered plugins** —
    open ABCs in core, commercial implementations in a proprietary package.
