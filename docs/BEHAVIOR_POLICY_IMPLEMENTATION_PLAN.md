# Redibis Behavior Policy Runtime — Implementation Handover Plan

**Status:** Phases 0–9 implemented in open-core (PII MVP + lifecycle + UI + plugins +
learning). Post-review hardening landed for path safety, active-policy wiring,
simulation receipts, rollback stack pop, checksum fallthrough, and agent/plugin
parity. Default remains `behavior.enabled: false`.  
**Audience:** an engineer or coding LLM starting with no conversation context.  
**Design authority:** `docs/BEHAVIOR_POLICY_ARCHITECTURE.md`.  
**Repository authority:** `CLAUDE.md` and `AGENTS.md`; their invariants override this plan.
**Operator CLI tour:** `docs/CLI_SCAN_ENRICH_TOUR.md` §5 (`redibis behavior …`).

This plan turns the recommended generalized behavior-policy architecture into small,
independently shippable phases. Do not jump directly to UI or arbitrary plug-ins.
First make existing edge-rule behavior typed, consistent, replayable, and observable.

Each task includes:

- **Context** — relevant existing behavior.
- **Goal** — the intended design outcome.
- **Achieve** — concrete work.
- **Why** — architectural reason.
- **Tips** — implementation constraints.
- **Tests** — minimum verification.

---

## Implementation status (living)

| Phase | Scope | State |
|---|---|---|
| 0 | Edge-rule hardening, overlay permission, parity corpus | ✅ |
| 1 | Models, schema, compiler, lint | ✅ |
| 2 | Registries, conditions, evaluator, reducer | ✅ |
| 3 | Audit, local store, `BehaviorConfig` | ✅ |
| 4 | PII adapter + pipeline shadow/active + edge compat | ✅ |
| 5 | REST lifecycle, simulation, approve/activate/rollback | ✅ |
| 6 | Settings Behavior tab, sim/activation/audit UI | ✅ |
| 7 | Plug-in SDK, allowlist, conformance, telecom reference | ✅ |
| 8 | Classification / quality / profiling / masking adapters | ✅ library; **not** in main scan pipeline |
| 9 | Outcome labels, signals, draft-from-corrections | ✅ (propose-only) |
| CLI | `redibis behavior …` | ✅ |

### Runtime resolution (PII)

When `behavior.enabled` and mode is `shadow`/`active`:

1. Compile policies from `behavior.policies` (file paths or store IDs).
2. Load **active** policies from the behavior store (`engine=pii`, matching stage).
3. If none → convert the classification pack’s `edge_rules` to a compatibility
   `BehaviorPolicy` (`edge_compat:<pack>`).
4. Consult durable `PiiDecisionStore` overlays so human decisions outrank policy.
5. Checksum-protected entity changes fall through to the next rule (parity with
   `edge_rules._checksum_blocks_rule`).
6. `fail_mode: fail_run` re-raises; `baseline_with_warning` keeps the baseline
   detection and surfaces a visible warning (pipeline no longer swallows `fail_run`).

### Lifecycle gates (enforced)

- Policy/version/rule IDs must be path-safe (`a-zA-Z0-9._-`); traversal is rejected.
- Approve/activate require a **durable simulation receipt** for that id/version/SHA
  (forged `simulation_id` strings are rejected).
- Rollback **pops** the activation stack (second rollback continues backward).
- REST: `/active` and `/history` are registered before `/{policy_id}/{version}`.

### Known remaining gaps

1. **Non-PII adapters** — call from library/tests; not hooked in `pipeline.py`.
2. **API authz** — mutating routes still trust client-supplied `actor`/`role`
   (same pattern as other settings endpoints); bind to session auth when available.
3. **Packaged edge YAML** remains the authoring format for telecom packs; Behavior
   Policy documents are the governed product surface for new rules.
4. Do not treat learning drafts as auto-activation — human approve + simulate still required.

Package layout: `redibis/behavior/` (runtime), `redibis/services/behavior_service.py`,
`redibis/webapp/behavior_routes.py`, `redibis/cli/behavior_cmd.py`.

---

## PART 1 — ORIENTATION

### 1.1 Mission

Let Redibis users correct recurring false positives and false negatives without
editing engine base code.

Users author safe YAML/JSON rules. Rules read registered facts, execute registered
actions, and produce typed patches at explicit engine hook stages. Trusted deployment
packages may register precompiled actions. Every applied patch is deterministic,
bounded, reversible, and auditable.

### 1.2 Non-goals for the first release

- No inline Python in policy files.
- No free-form expression language.
- No raw cell values in policy context.
- No direct contract writes from behavior actions.
- No replacement of durable PII/quality/definition decision overlays.
- No simultaneous integration of every Redibis domain.
- No policy-driven execution of arbitrary agent dynamic tools.
- No behavioral change when policies are disabled.

### 1.3 Existing implementation to reuse

| Concern | Existing source |
|---|---|
| Safe declarative edge rules | `redibis/classification/edge_rules.py` |
| Policy packs and user copies | `redibis/classification/policy_pack.py`, `pack_store.py` |
| Shared PII pipeline | `redibis/services/pipeline.py` |
| Evidence/verdict separation | `redibis/pii/detector.py`, `equations.py`, `models.py` |
| Per-run editable overrides | `redibis/pii/regex_overrides.py`, `scan/config.py` |
| Engine entry-point pattern | `redibis/profiling/registry.py`, `quality/registry.py` |
| Durable authority overlays | `redibis/store/pii_decisions.py`, `quality_decisions.py` |
| Operational telemetry | `redibis/store/contract_metadata.py` |
| Sanitized decision audit | `redibis/obs/decision.py` |
| Agent audit/HITL | `redibis/agents/run_log.py`, `executor.py`, `review_queue.py` |
| Generated-code safety | `redibis/contracts/rule_code_parser.py`, `agents/dynamic_sandbox.py` |

### 1.4 Hard invariants

1. `ContractStore.upsert()` remains the only contract-bucket writer.
2. The detector produces evidence; verdict layers set `detected`.
3. Regex catalog remains pure data and never imports Presidio.
4. Behavior context and audit contain no raw values.
5. Human decision overlays outrank automated policy.
6. Passing authoritative validators and safety/legal guards outrank behavior policy.
7. LLM-generated policy is suggest-only until validation, simulation, and approval.
8. Every model call remains behind `guarded_model_call`.
9. Domain packages do not import CLI or agent-framework modules.
10. Masking policy is intent; enforcement remains external.
11. Lawful intercept is detected/escalated, never applied.
12. Generated code is proposed, scanned, approved, and sandboxed.

### 1.5 Recommended package boundary

Create a framework-neutral package:

```text
redibis/behavior/
  __init__.py
  models.py
  schema.py
  compiler.py
  registry.py
  conditions.py
  evaluator.py
  reducer.py
  audit.py
  config.py
  store.py
  adapters/
    __init__.py
    pii.py
    classification.py       # later phase
    quality.py              # later phase
    profiling.py            # later phase
    masking.py              # later phase
```

The core package may import stable shared models/config utilities. It must not import
Presidio, GE, LangGraph, CopilotKit, CLI code, or web code.

---

## PART 2 — LOCKED DESIGN DECISIONS

Treat these as implementation requirements unless a product owner explicitly changes
them:

1. Policies are YAML/JSON data compiled into immutable Python models.
2. Policies use registered identifiers, not arbitrary object paths or callables.
3. Hook stages are `pre_evidence`, `post_evidence`, `pre_verdict`, `post_verdict`,
   `pre_action`, and `post_action`.
4. Actions return `BehaviorPatch`; they do not mutate engine objects or perform I/O.
5. Adapters declare capabilities and own patch application.
6. Conditions use `TRUE/FALSE/UNKNOWN`.
7. The public v1 rule is `when + effects + reason + priority + terminal`. Engine, hook,
   and scope are policy-level metadata.
   `reason` is mandatory and non-empty; effect-level review reasons are supplemental.
8. Unknown facts are fixed to `no_match` in v1; authors do not select per-rule behavior.
9. Incompatible scalar verdict/security effects are fixed to review in v1; authors do
   not select a conflict policy.
10. Authority and policy-layer precedence are derived by the compiler/runtime and are
    not authored rule fields.
11. Authority order is:
   safety guard > validator > human decision > approved policy > engine native >
   LLM proposal.
12. Content SHA-256 identifies the exact normalized policy.
13. Existing edge rules remain compatible during migration.
14. The first authoritative PII adapter includes `post_verdict` corrections and one
    bounded `pre_verdict` threshold effect for false-negative tuning.
15. Linting and shadowed-rule analysis ship in v1 and gate activation.
16. Product users do not upload executable Python.
17. Trusted plug-ins are installed packages loaded from explicit entry points and an
    allow-list.
18. Operational policy/audit data stays outside active ODCS contracts.

---

## PART 3 — CLARIFICATIONS AND SAFE DEFAULTS

These questions may be answered before implementation. If they are unanswered, use the
listed default so work can proceed.

### C1. Is multi-tenancy required?

**Default:** include optional `tenant` fields in scope and audit models, but do not
require tenant-aware storage in v1.

### C2. Who approves production policy?

**Default:** Steward for normal behavior; preserve existing DPO/SecOps/Legal routing
when actions or resulting tags require those roles.

### C3. Where are policy versions stored?

**Default:** reuse the existing config-store abstraction with a new behavior-policy
namespace. Keep runtime/compiler independent of local versus object storage.

### C4. Which regex engine is safe enough?

**Default:** define a `matches` operator abstraction. Initially use a bounded
implementation with explicit runtime protection; do not present regex length as a
complete timeout defense.

### C5. Can multiple matching rules apply?

**Default:** yes. Sort by priority descending and stable fully qualified ID. Combine
compatible additive patches. A rule stops later evaluation only when
`terminal: true`. Incompatible scalar effects route to review.

### C6. Is canary rollout required?

**Default:** provide `disabled`, `shadow`, and `active` modes. Defer percentage rollout.

### C7. Must plug-ins be cryptographically signed?

**Default:** require deployment allow-listing and capture package/version/digest.
Leave signature enforcement behind an interface for enterprise deployments.

### C8. What historical data may simulation retain?

**Default:** retain normalized value-free `BehaviorContext` snapshots and approved
labels only. Never retain raw samples for the behavior-policy feature.

---

## PART 4 — PHASED BUILD PLAN

## Phase 0 — Baseline, parity, and current-edge-rule hardening

**Outcome:** existing behavior is understood, consistent, and test-protected before
extracting a generalized runtime.

### T0.1 — Build a behavior regression matrix

**Context**

Current edge rules are tested primarily in `tests/test_edge_rules.py` and detection
accuracy tests. Modern and legacy PII paths are not structurally identical.

**Goal**

Create a compact regression corpus that represents:

- every built-in edge rule;
- matching and non-matching cases;
- checksum-protected cases;
- no-profile/missing-fact cases;
- broad-rule shadowing;
- per-run overlay replacement;
- modern and legacy path behavior.
- known pack quirks: `SubscriberPII`→MSISDN co-tag bleed, `secret`/`_hash$`
  substring over-match, and `imei_history_json` matching the IMEI name rule.

**Achieve**

- Add test fixtures that produce normalized inputs and expected final detections.
- Parameterize tests across supported PII entry paths where those paths are meant to
  be equivalent.
- Record current rule ID and `decision_path`.

**Why**

The generalized runtime must prove behavioral parity before becoming authoritative.

**Tips**

- Do not “fix” behavior while capturing baseline unless it is clearly a current bug and
  receives a separate test.
- Avoid relying on optional NER model availability.

**Tests**

- Existing edge-rule and detection-accuracy tests.
- New parity test file, suggested:
  `tests/test_behavior_edge_rule_parity.py`.

### T0.2 — Complete current edge-rule value validation

**Context**

Current validation rejects unknown keys and invalid regex syntax but does not fully
validate every value type, range, enum, and action combination.

**Goal**

Fail closed before a current pack is accepted.

**Achieve**

- Validate booleans as booleans, not truthy strings.
- Validate rates within `[0, 1]`.
- Validate ranges as two finite numbers with `low <= high`.
- Validate tags as bounded non-empty strings.
- Validate entity and classification/action enums where authoritative catalogues exist.
- Validate rule-level unknown keys, not only `when`/`then`.
- Enforce document/rule/string limits.

**Why**

The compatibility source must be safe and predictable before conversion.

**Tips**

- Preserve existing valid packs.
- Return path-specific `ConfigError` messages.

**Tests**

- Add negative tests for each malformed value category.

### T0.3 — Resolve declared-action consumption

**Context**

Some `RefineResult` fields are returned but not applied in the main PII path.

**Goal**

No accepted action may silently do nothing.

**Achieve**

For each existing action:

- either wire an explicit downstream consumer;
- or reject it for the current PII adapter until a supported stage exists.

Add tests proving final output, not just `RefineResult`, changes as documented.

**Why**

Silent no-op actions make policy audit misleading.

**Tips**

- Do not push telemetry-only fields into the contract spec.
- Preserve contract-writer and decision-overlay boundaries.

### T0.4 — Enforce per-run overlay permission

**Context**

`classification.per_run_overlay_allowed` is configured but not consistently enforced.

**Achieve**

- Reject an overlay at service/API config construction when disabled.
- Recheck permission at runtime compilation.
- Audit accepted overlay identity/SHA.

**Tests**

- Disabled overlay is rejected across library, web/session, CLI, and agent paths that
  expose it.

### T0.5 — Converge supported PII paths

**Context**

Legacy Workflow B bypasses current edge rules.

**Goal**

Supported paths call the same post-equation behavior seam or explicitly document that
legacy behavior differs.

**Achieve**

- Prefer delegation to `services.pipeline.run_pii_detection`.
- If compatibility prevents delegation, extract a shared post-verdict function and call
  it from both paths.

**Tests**

- Regression corpus passes through both intended paths.

### Phase 0 definition of done

- Existing suite is green.
- Current packs receive stronger validation.
- No accepted action is silently ignored.
- Overlay permission is enforced.
- Supported PII paths have explicit parity.
- No generalized package exists yet.

---

## Phase 1 — Core object model and schema

**Outcome:** policies can be parsed and compiled, but cannot change engine behavior.

### T1.1 — Add immutable core models

**Context**

See the object model in `docs/BEHAVIOR_POLICY_ARCHITECTURE.md`.

**Goal**

Create framework-neutral, JSON/YAML-safe models in `redibis/behavior/models.py`.

**Achieve**

Implement:

- `HookStage`;
- `TruthValue`;
- `UnknownBehavior`;
- `ConflictPolicy`;
- `Authority`;
- `PolicySource`;
- `RuleScope`;
- `Predicate`;
- `ConditionGroup`;
- `EffectCall`;
- `AuthoredRule` with only `when`, `effects`, `reason`, `priority`, and `terminal`
  plus a stable ID;
- internal `CompiledRule` with compiler-derived authority, unknown handling, conflict
  handling, source layer, engine, hook, and scope;
- `BehaviorPolicy`;
- `BehaviorPolicySet`;
- `BehaviorContext`;
- `PatchOperation`;
- `BehaviorPatch`;
- trace/result models.

**Why**

Typed immutable boundaries prevent arbitrary engine mutation and stabilize future
adapters.

**Tips**

- Avoid `Any` in public fields except validated JSON values.
- Use tuples/frozen dataclasses in compiled objects.
- Keep runtime implementations/callables out of serializable models.
- Do not leak internal enforcement fields into the public authoring schema.

**Tests**

- Equality/immutability tests.
- JSON-safe round-trip for serializable representations.
- Enum and path validation.

### T1.2 — Define policy document schema

**Goal**

Validate raw YAML/JSON structurally before compilation.

**Achieve**

- Add `apiVersion`, `kind`, `metadata`, `appliesTo`, and `rules`.
- Put engine, hook, and ordinary scope in policy-level `appliesTo`; avoid repeating them
  on every v1 rule.
- Restrict the public rule schema to stable ID, `when`, `effects`, `reason`, optional
  `priority`, and optional `terminal`.
- Require a non-empty bounded `reason` on every rule. Copy it into match traces,
  patches, simulation output, and audit events. `core.review.add_reason` may add
  context but cannot satisfy the rule-level requirement.
- Add Pydantic or equivalent schema models in `behavior/schema.py`.
- Add hard limits for size, depth, counts, and strings.
- Reject unknown keys.

**Why**

Structural validation gives clear author feedback and prevents permissive config drift.

**Tips**

- Keep schema models separate from immutable runtime models.
- Never use `eval`, `exec`, `compile`, or dynamic imports.

**Tests**

- Valid examples.
- Unknown field rejection.
- Excessive depth/count/size rejection.
- Duplicate IDs.
- Missing, blank, or oversized rule reason.

### T1.3 — Add the v1 policy linter

**Goal**

Detect ordering and authoring mistakes before any policy can activate. This is an MVP
compiler capability, not a Phase-6 editor enhancement.

**Achieve**

Add structured lint findings with severity, policy/rule IDs, explanation, and suggested
fix. At minimum detect:

- unreachable or fully shadowed rules;
- terminal rules shadowing lower-priority rules;
- duplicate/equivalent conditions;
- same-priority incompatible scalar effects;
- broad rules preceding narrower rules;
- facts unavailable at the selected engine/hook;
- effects unsupported by the selected engine/hook;
- overlapping scopes with conflicting effects;
- rules that can only evaluate to unknown;
- missing/empty reason;
- custom effects that duplicate a built-in effect.

Implement the lint result model and structural/order checks in Phase 1. Checks that
depend on fact/effect capabilities become active as soon as registries land in T2.1;
all listed checks are mandatory by the Phase-2 definition of done.

Activation policy:

- errors and conflicts block activation;
- shadowing requires explicit acknowledgement and successful simulation;
- broad/overlap warnings require simulation.

**Why**

Priority and terminal behavior are simple enough for operators only when tooling
exposes effective ordering and prevents accidental shadowing.

**Tests**

- One focused test per lint category.
- Stable finding IDs and ordering.
- Valid policies produce no blocking findings.

### T1.4 — Canonical normalization and content digest

**Goal**

The same semantic document produces the same content SHA.

**Achieve**

- Normalize defaults, enum strings, rule order metadata, and mappings.
- Serialize canonical JSON with stable key ordering.
- Compute SHA-256.
- Store digest in compiled policy.

**Why**

Audit, replay, cache correctness, and rollback depend on immutable identity.

**Tests**

- YAML formatting/key-order changes do not change SHA.
- Semantic changes do change SHA.

### T1.5 — Scope matching

**Achieve**

Implement engine, stage, table, column, environment, jurisdiction, tenant, and
effective-time matching.

**Tips**

- Use bounded glob semantics for table/column scope.
- Do not use arbitrary regex for all scope matching.
- Inject evaluation time into compilation/run context for deterministic tests.

**Tests**

- Inclusive/exclusive scope cases.
- Effective-date boundaries.
- Missing optional scope values.

### Phase 1 definition of done

- Policy documents compile to immutable models and SHA.
- Public rules expose only the simplified authoring fields.
- Lint findings exist before runtime integration and blocking findings prevent
  activation.
- No engine code imports the new package yet.
- Invalid policies fail closed with useful paths.
- Core has no framework or domain-library dependency.

---

## Phase 2 — Registries, conditions, evaluator, and reducer

**Outcome:** the neutral runtime evaluates synthetic contexts and returns deterministic
patches/traces.

### T2.1 — Build registry descriptors

**Goal**

Create metadata-first registries for facts, operators, and effects. A trusted
precompiled action is exposed to authors as a registered effect ID.

**Achieve**

- `FactDescriptor`
- `OperatorDescriptor`
- `EffectDescriptor`
- registration functions;
- collision policy;
- catalogue serialization;
- registry manifest digest.
- compile-time binding from each referenced fact/effect ID to its descriptor;
- fail-closed rejection of unknown IDs and experimental IDs unless the policy explicitly
  opts into experimental capabilities.

**Why**

Descriptors support validation, UI generation, audit, and plug-in discovery without
exposing implementations.

**Tips**

- Built-ins win on collisions.
- Reject duplicate third-party IDs.
- Require namespaced external IDs.

**Tests**

- Registration, duplicate handling, catalogue output, manifest SHA.

### T2.2 — Implement built-in operators and three-valued logic

**Achieve**

Implement type-safe operators from the architecture:

- comparison;
- membership;
- string;
- existence;
- bounded `matches`.

Implement `all`, `any`, and `not` truth tables for `TRUE/FALSE/UNKNOWN`.

**Why**

Missing engine facts must not accidentally trigger or suppress policy.

**Tests**

- Complete truth-table tests.
- Operator type mismatch.
- null/missing facts.
- regex budget/error behavior.

### T2.3 — Implement effect protocol and built-in patch effects

**Goal**

Effects return patches without I/O or mutation.

**Initial built-ins**

- `core.verdict.set_detected`;
- `core.verdict.set_entity`;
- `core.verdict.set_confidence`;
- `core.review.require`;
- `core.review.add_reason`;
- `core.threshold.set_for_run` as a bounded effect contract, with adapter-owned clamp
  limits and mandatory review metadata for negative→positive promotions;
- selected candidate effects only if an adapter can support them safely.

**Tips**

- Keep domain entity validation in adapter or a registered validator.
- Do not add a generic `set_path` action to the public catalogue.

**Tests**

- Parameter-schema validation.
- Pure deterministic results.
- Unsupported parameters rejected.

### T2.4 — Implement evaluator

**Achieve**

- Filter by policy/rule scope and stage.
- Sort by priority and fully qualified ID.
- Evaluate all applicable rules unless a matching rule is `terminal`.
- Execute effects for matching rules.
- Treat unknown facts as no-match in v1.
- Return full rule trace.

**Trace must distinguish**

- out of scope;
- condition false;
- unknown/no-match;
- matched;
- effect error;
- terminated;
- shadowed/blocked later by reduction.

**Tests**

- Stable ordering.
- Multiple matches.
- terminal behavior.
- unknown no-match behavior.
- effect failure.

### T2.5 — Implement capability-aware patch reducer

**Achieve**

- Validate operation against adapter capabilities.
- Validate path/value.
- Apply authority ordering.
- Detect conflicts.
- Combine additive operations deterministically.
- Return proposed/applied/blocked patches.
- Consult supplied human-decision authority at reduction time so scan-time audit records
  policy operations blocked by durable overlays.

**Why**

The reducer is the enforcement boundary between policy and engine behavior.

**Tests**

- Validator blocks policy.
- Human decision blocks policy.
- Policy overrides engine-native verdict when allowed.
- LLM proposal cannot override policy.
- incompatible verdict operations route to review by default.
- unsupported path is rejected.
- a policy cannot resurrect a column already demoted by a human PII overlay.

### Phase 2 definition of done

- Synthetic contexts evaluate deterministically.
- The evaluator emits the effective order and shadow/terminal trace required by the
  v1 linter and simulation API.
- All structural and registry-aware v1 lint checks are implemented; none are deferred
  to the Phase-6 editor.
- No adapter applies patches to production domain models.
- Full structured traces exist.
- Registry and policy digests are stable.

---

## Phase 3 — Audit, policy storage, and runtime configuration

**Outcome:** policies and evaluations are identifiable, storable, and observable before
they affect engines.

### T3.1 — Add behavior configuration

**Achieve**

Add a `BehaviorConfig` under `RedibisConfig`, default disabled:

```yaml
behavior:
  enabled: false
  mode: disabled          # disabled | shadow | active
  engine_modes: {}        # e.g. {pii: shadow}; per-engine circuit breaker
  policies: []
  allow_per_run_overlay: false
  plugin_allowlist: []
  fail_mode: baseline_with_warning  # baseline_with_warning | fail_run
  max_rules_per_context: 200
  max_eval_ms: 50
```

**Clarification**

`baseline_with_warning` means preserve baseline engine behavior on an ordinary
evaluator failure and mark the run with a visible warning. It is not silent fail-open.
The warning must reach the library/service result, CLI/web progress or summary,
run artifact, and structured audit/metrics. `fail_run` is a policy/deployment option
for cases where reintroducing a known FP/FN is less acceptable than failing the run.
Safety/legal guard failure remains fail closed in every mode.

**Tests**

- Default config does not alter existing behavior.
- YAML round-trip and validation.
- Global and per-engine circuit breakers disable evaluation immediately.
- `baseline_with_warning` returns the baseline and a visible run warning.
- `fail_run` stops the governed run with a policy-specific error.

### T3.2 — Add policy store abstraction

**Goal**

Store immutable versions and an activation pointer outside contracts.

**Achieve**

- Interface for list/get/save/activate/deactivate.
- Local implementation.
- Object-storage implementation if existing config-store abstraction supports it cleanly.
- Prevent mutation of an existing `(id, version, SHA)` record.

**Why**

Policy lifecycle is configuration governance, not contract state.

### T3.3 — Emit structured policy audit events

**Achieve**

Add a `PolicyAuditEvent` and sink abstraction.

Initial sinks:

- run artifact JSONL;
- sanitized decision logger/telemetry integration.

Include:

- run/table/column;
- engine/stage;
- policy/rule/action IDs;
- mandatory rule reason plus optional effect-added detail;
- policy and registry SHA;
- before/proposed/applied/blocked hashes or sanitized bodies;
- duration and outcome.

**Tests**

- raw-value-like input keys/strings are absent;
- stable hashes;
- matched and blocked event cases.

### T3.4 — Shadow-mode runtime service

**Goal**

Attach policies to a run and evaluate without applying patches.

**Achieve**

- Compile effective policy set once per run/SHA.
- Provide no-policy fast path.
- Emit comparison trace in `shadow` mode.
- Include lint findings, effective rule order, terminal stops, and shadowed rules in
  shadow output.
- Add time/count budgets and timeout outcome.
- Add a structured `BehaviorPolicyWarning` to run/service results. On ordinary active
  evaluation failure in `baseline_with_warning` mode, preserve baseline output and
  surface the warning through progress callbacks, CLI/web summaries, run artifacts,
  audit, and metrics.

**Tests**

- Disabled mode does not evaluate.
- Shadow mode never changes domain output.
- Active mode for PII is wired in Phase 4 (`services/pipeline.py`); other engines stay
  library-only until their pipeline hooks land.
- Baseline fallback cannot occur with only a log/metric; a caller-visible warning is
  asserted.

### Phase 3 definition of done

- Behavior defaults off.
- Policies can be versioned and activated.
- Shadow evaluation is audited.
- No contract or domain behavior changes.

---

## Phase 4 — PII compatibility adapter and authoritative migration

**Outcome:** existing edge rules run through the new runtime with parity.

### T4.1 — Implement PII fact adapter

**Context**

`EdgeRuleColumnContext` currently builds facts from `PIIDetection`, optional profiles,
and aggregate dataframe-derived validators.

**Goal**

Produce a value-free `BehaviorContext` for:

- `post_evidence`;
- `pre_verdict`;
- `post_verdict`.

**Achieve**

Register stable PII facts:

- column metadata;
- profile rates;
- engine scores/states;
- checksum statuses;
- phone valid rate/regions;
- current verdict/entity/confidence;
- review and authority metadata.

**Tips**

- Bind to aggregate facts already present on `PIIDetection`, profile results, or engine
  state before computing anything new.
- Do not rerun checksum scans, phone validation, or profiling during policy evaluation
  when detection already produced the value.
- If a required aggregate is genuinely absent, compute it once in the owning
  detection/profile stage and carry it forward; never expose the dataframe in context.

**Tests**

- Context contains no raw values.
- Missing profile/engine facts are represented as unavailable.

### T4.2 — Implement legacy edge-rule converter

**Goal**

Compile current `ClassificationPolicy.edge_rules` into v1 `BehaviorPolicy`.

**Mapping**

- current allow-listed `when` keys → fact/operator predicates;
- `then` keys → registered effects;
- list order → stable compatibility priority/order;
- checksum protection → validator authority/guard;
- first-match-wins → compatibility `terminal: true`;
- current ID and note preserved.

**Why**

Existing user and built-in packs must not be rewritten immediately.

**Tests**

- Every existing valid rule converts.
- Every current regression fixture yields equivalent `RefineResult`/detection.

### T4.3 — Implement PII patch application

**Goal**

Apply supported reduced patches immutably to `PIIDetection`.

**Achieve**

- set entity/detected/confidence within capability constraints;
- require review;
- append compatibility `decision_path`;
- return structured trace separately;
- preserve detector/equation invariant.
- consult relevant `PiiDecisionStore` state at scan time and supply it to the reducer as
  `HUMAN_DECISION` authority; keep `ContractStore.upsert()` reconciliation as the
  write-time backstop.

**Tests**

- NOT_PII demotion.
- entity promotion.
- checksum-protected block.
- review requirement.
- unsupported current action is caught, never silent.
- a policy cannot resurrect a human-overlay-demoted column.

### T4.4 — Add bounded PII pre-verdict threshold effect

**Goal**

Include a real false-negative tuning lever in the first authoritative PII release
instead of supporting only post-verdict false-positive corrections.

**Achieve**

- Add `core.threshold.set_for_run` to the PII `pre_verdict` adapter.
- Permit only named PII thresholds exposed by capabilities.
- Clamp each value to hard adapter-owned minimum/maximum bounds.
- Apply only to the current run/context; never mutate global configuration.
- Mark a negative→positive promotion caused by the adjustment for review.
- Audit baseline threshold, requested value, clamped value, and resulting verdict
  without raw data.

**Why**

Relabelling an existing verdict cannot recover evidence rejected by an overly strict
threshold. A bounded pre-verdict effect is the smallest safe FN capability.

**Tests**

- in-range threshold adjustment;
- below/above bound clamps;
- unsupported threshold rejection;
- run isolation;
- promotion requires review;
- validator/safety authority still wins.

### T4.5 — Dual-run parity

**Achieve**

In shadow mode:

- run old edge evaluator;
- run new converter/runtime;
- compare domain result and matched rule;
- emit parity mismatch artifact.

Run against:

- unit corpus;
- representative reviewed run contexts if available and value-free.

**Exit gate**

No unexplained parity mismatches.

### T4.6 — Switch PII authority

**Achieve**

- New runtime becomes authoritative when `behavior.mode=active`.
- Existing `edge_rules_enabled` continues to work through compatibility translation.
- Disabled behavior preserves prior path during deprecation window.
- Avoid double-application.
- Enable both bounded `pre_verdict` threshold effects and compatible `post_verdict`
  corrections.

**Tests**

- all PII and edge-rule tests;
- modern/legacy path parity;
- disabled/default compatibility;
- structured audit/provenance.
- scan-time overlay block and write-time reconciliation parity.

### Phase 4 definition of done

- Existing packs work unchanged.
- New runtime can authoritatively apply bounded PII pre-verdict threshold effects and
  post-verdict corrections.
- Validator/human/safety precedence is proven.
- No raw values or direct contract writes.

### MVP boundary after Phase 4

Phases 0–4 are the first product release: hardening, neutral core, v1 lint/trace,
shadow/parity, and authoritative PII FP/FN tuning. Phases 5–9 are product maturity
and are now also in-tree (see **Implementation status** at the top of this document).
Do not regress Phase-0 fixes or the Phase-4 PII MVP when extending adapters.

---

## Phase 5 — Policy APIs, simulation, and approval lifecycle

**Outcome:** a governed backend product surface exists before a rich editor.

### T5.1 — Catalogue and validation APIs

Add:

```text
GET  /api/behavior/catalog
POST /api/behavior/policies/validate
```

Return path-specific validation errors, v1 lint findings, effective rule order, and a
normalized preview/SHA. Blocking lint findings make the document ineligible for
activation even before the visual editor exists.

**Tests**

- authz behavior follows existing settings/config endpoints;
- unknown registry references rejected;
- catalogue is deterministic;
- shadow/conflict lint findings follow the Phase-1 activation policy.

### T5.2 — Policy CRUD/version APIs

Add immutable create/list/get operations. Saving the same ID/version with different
content must fail.

**Do not**

- overwrite built-in packaged policy files;
- write policy into active contracts.

### T5.3 — Simulation API

Add:

```text
POST /api/behavior/policies/simulate
```

Input options:

- explicit sanitized context;
- run/table reference that the server converts to sanitized contexts.

Output:

- matches/non-matches/unknown;
- proposed/applied/blocked patch;
- conflicts;
- before/after verdict;
- aggregate impact.

Simulation never writes contracts.

### T5.4 — Approval and activation

Add:

```text
POST /api/behavior/policies/{id}/{version}/approve
POST /api/behavior/policies/{id}/{version}/activate
POST /api/behavior/policies/{id}/{version}/deactivate
POST /api/behavior/policies/{id}/rollback
```

Record actor, role, note, prior/new SHA, and simulation reference.

**Default**

Production activation requires approval and successful simulation.

### T5.5 — Promote correction to draft policy

Generalize `suggest_rule_from_correction`:

- accept approved human correction;
- produce a narrow draft rule;
- include source run/table/column metadata outside executable conditions;
- require validation/simulation/approval.

Do not auto-activate.

### Phase 5 definition of done

- Policy lifecycle is backend-complete and audited.
- Simulation is write-free.
- Rollback changes active pointer, not immutable history.
- LLM/human promotion creates drafts only.

---

## Phase 6 — Structured editor and operational visibility

**Outcome:** users can safely author and understand policies without hand-editing YAML.

### T6.1 — Add policy settings page

Build from `/api/behavior/catalog`.

Editor capabilities:

- policy metadata and scope;
- ordered rule list with explicit priority;
- condition-tree builder;
- action forms generated from parameter schema;
- raw YAML/JSON view;
- validation results;
- compiler-provided lint findings and effective rule order;
- built-in versus user policy distinction.

The editor visualizes the v1 linter; it must not be the first implementation of
shadow/conflict analysis.

Do not change existing manual UI behavior when `behavior.enabled=false`.

### T6.2 — Add simulation/impact view

Show:

- affected tables/columns;
- baseline versus proposed verdict;
- matching rule/reason;
- blocked operations;
- conflicts and unknown facts;
- estimated FP/FN change where reviewed labels exist.

### T6.3 — Add activation and rollback controls

Require role and note. Show active SHA/version and last simulation summary.

### T6.4 — Add audit explorer

Filters:

- run/table/column;
- engine/stage;
- policy/rule/action;
- outcome;
- blocked/conflict;
- time range.

Keep raw values out of UI payloads.

### T6.5 — Add metrics

At minimum:

- evaluations;
- rule matches;
- no matches;
- unknown facts;
- blocked operations;
- conflicts/reviews;
- errors/timeouts;
- latency;
- parity mismatches during migration;
- policy-corrected outcomes later approved/rejected by humans.
- use of custom plug-in effects and repeated verbose condition shapes, so product can
  identify missing built-in predicates/effects before considering a mini-language.

### Phase 6 definition of done

- Users can author, validate, simulate, approve, activate, observe, and roll back.
- UI is catalogue-driven.
- Behavior-off UI safety tests pass.

---

## Phase 7 — Trusted fact/action plug-in SDK

**Outcome:** deployments can add precompiled behavior without modifying Redibis core.

### T7.1 — Define entry-point protocols

Suggested groups:

```text
redibis.behavior_facts
redibis.behavior_actions
```

Each entry point returns descriptors and pure providers/handlers.

### T7.2 — Add plug-in allow-list and manifest

Manifest fields:

- distribution;
- version;
- entry-point name;
- registered IDs;
- package/source digest where available;
- side-effect/autonomy declarations.

Reject:

- unallow-listed packages;
- built-in ID replacement;
- unsupported side-effect classes;
- missing descriptors.

### T7.3 — Add plug-in conformance suite

Provide tests/helpers that verify:

- deterministic repeated output;
- JSON-safe patch;
- no context mutation;
- only declared operations;
- parameter-schema behavior;
- no raw-value requirement;
- bounded execution.

### T7.4 — Add one reference domain plug-in

Choose a non-sensitive, pure example such as telecom identifier normalization.

The plug-in:

- reads only registered metadata facts;
- returns a patch;
- has no I/O;
- is referenced by YAML action ID.

### T7.5 — Decide process isolation

**Default v1**

Installed allow-listed plug-ins execute in-process because they are deployment code.

**Follow-up decision**

If untrusted third-party plug-ins are required, add process/container isolation rather
than weakening the product policy format.

### Phase 7 definition of done

- A deployment package adds behavior through an entry point.
- No core file changes are required.
- Manifest, allow-list, digest, audit, and conformance tests exist.

---

## Phase 8 — Additional engine adapters

Add adapters one at a time. Each adapter requires a capability document, threat review,
tests, and audit examples.

### T8.1 — Classification candidate adapter

Hooks:

- `post_evidence` after ensemble collection;
- `post_verdict` after policy resolution;
- `post_action` for approval escalation.

Permitted first actions:

- add/drop candidate tag;
- require review;
- add reason.

Never bypass `apply_forbidden`, golden rules, or role routing.

### T8.2 — Quality proposal adapter

Operate on quality proposals, not raw GE internals.

Permitted first actions:

- suppress proposal;
- change severity;
- require review.

Durable changes continue through `QualityDecisionStore`.

### T8.3 — Profiling triage adapter

Permitted first actions:

- require deep profile;
- select installed profiler before run;
- adjust bounded run-scoped triage threshold.

Do not mutate profiler result dictionaries arbitrarily.

### T8.4 — Masking recommendation adapter

Permitted first actions:

- choose suggested strategy from allowed capabilities;
- change plan parameters within schema;
- require approval.

Never expose keys or perform external enforcement.

### T8.5 — Agent typed-tool integration

Agents may:

- select active policy;
- propose draft policy;
- run validation/simulation;
- create review item.

Agents may not:

- auto-activate LLM-generated policy by default;
- call plug-in implementation directly;
- bypass capability registry.

### Phase 8 definition of done

- Each adapter is independently guarded by feature configuration.
- Domain invariants and existing overlays remain authoritative.
- Typed tools expose policy operations to agents.

---

## Phase 9 — Learning loop and accuracy governance

**Outcome:** behavior policies improve measured quality without becoming self-modifying.

### T9.1 — Capture policy outcome labels

When a human later approves/rejects a policy-influenced verdict, record:

- policy/rule/SHA;
- original engine verdict;
- policy verdict;
- human result;
- value-free format fingerprint/context hash.

### T9.2 — Compute policy precision signals

Per rule:

- applications;
- human confirmations;
- human reversals;
- unknown/conflict rate;
- estimated precision delta;
- table/domain distribution.

Do not call these full precision/recall unless labelled ground truth coverage supports
that claim.

### T9.3 — Suggest narrowing or retirement

Rules with high reversal or broad impact create a review suggestion. They are never
automatically rewritten or disabled by an LLM.

### T9.4 — Draft from repeated corrections

Repeated compatible corrections may generate a draft with:

- narrow scope;
- evidence guards;
- source statistics;
- simulation report.

Activation remains human/policy gated.

### Phase 9 definition of done

- Policy quality is measurable.
- Learning produces proposals, never self-modifying active policy.
- Memory remains format-signature-only.

---

## PART 5 — FILE-BY-FILE INITIAL MAP

Expected new files:

```text
redibis/behavior/__init__.py
redibis/behavior/models.py
redibis/behavior/schema.py
redibis/behavior/compiler.py
redibis/behavior/registry.py
redibis/behavior/conditions.py
redibis/behavior/evaluator.py
redibis/behavior/reducer.py
redibis/behavior/audit.py
redibis/behavior/config.py
redibis/behavior/store.py
redibis/behavior/adapters/__init__.py
redibis/behavior/adapters/pii.py
tests/test_behavior_models.py
tests/test_behavior_schema.py
tests/test_behavior_registry.py
tests/test_behavior_conditions.py
tests/test_behavior_evaluator.py
tests/test_behavior_reducer.py
tests/test_behavior_audit.py
tests/test_behavior_pii_adapter.py
tests/test_behavior_edge_rule_parity.py
```

Expected existing files touched in early phases:

```text
redibis/config.py
redibis/classification/edge_rules.py
redibis/classification/policy_pack.py
redibis/services/pipeline.py
redibis/pii/runner.py
redibis/scan/config.py
redibis/obs/decision.py
redibis/store/config_store.py
redibis/store/contract_metadata.py
```

Later product phases may touch:

```text
redibis/webapp/backend.py
redibis/webapp/static/app.js
redibis/agents/node_registry.py
redibis/agents/tool_runner.py
redibis/agents/review_queue.py
```

Do not add behavior-policy writes to `ContractStore` except provenance/telemetry linkage
through existing supported methods.

---

## PART 6 — TEST STRATEGY

### 6.1 Unit tests

- schema and limits;
- canonical SHA;
- scope matching;
- registry collisions;
- three-valued logic;
- each operator;
- action parameter validation;
- evaluator ordering/stop/unknown;
- reducer authority/conflicts/capabilities;
- audit sanitization.

### 6.2 Compatibility tests

- every built-in edge rule;
- current user overlay behavior;
- checksum precedence;
- decision path;
- modern and legacy PII paths;
- behavior disabled.

### 6.3 Security tests

- unknown fields/actions/facts;
- expression/code injection strings;
- regex pathological patterns;
- oversized/deep policies;
- raw-value keys and value-like strings;
- plug-in collision;
- unallow-listed plug-in;
- forbidden patch path;
- attempt to write contract/external action.

### 6.4 Property tests

Recommended where practical:

- canonicalization determinism;
- evaluator repeatability;
- reducer order independence for compatible additive operations;
- no applied operation exceeds capabilities;
- serialization remains JSON-safe.

### 6.5 Integration tests

- config → compile → PII context → evaluate → reduce → apply → audit;
- simulation does not write;
- activation/rollback;
- contract provenance links policy SHA;
- decision overlay still wins;
- UI safety with behavior disabled.

### 6.6 Full suite

Run:

```bash
pytest tests/
```

Run focused tests during development, but no phase is complete until the full suite is
green or a pre-existing unrelated failure is explicitly documented.

---

## PART 7 — ROLLOUT PLAN

### Stage 1 — disabled

- Ship models/compiler/evaluator.
- Default `behavior.enabled=false`.
- No existing flow changes.

### Stage 2 — shadow

- Evaluate converted current edge policies.
- Compare to existing evaluator.
- Emit parity and latency reports.
- Do not apply new patches.

### Stage 3 — active for selected runs

- Enable PII post-verdict adapter for test/non-production scopes.
- Require explicit policy version/SHA.
- Monitor reversals, conflicts, and timeouts.

### Stage 4 — production PII

- Activate after parity and reviewed simulation.
- Keep immediate disable and rollback.
- Keep old edge format compatibility.

### Stage 5 — additional adapters

- Enable one domain at a time.
- Never activate a new adapter merely because the neutral runtime is stable.

---

## PART 8 — FAILURE AND RECOVERY SEMANTICS

### Ordinary evaluator failure

Default behavior:

- preserve baseline engine result;
- mark the run/result as degraded with a caller-visible `BehaviorPolicyWarning`;
- show policy/rule, stage, target, and baseline-fallback outcome in CLI/web summaries
  without raw values;
- persist the warning in run artifacts and emit structured audit/metrics;
- create a review item when the failed rule could have changed a verdict;
- do not fail the whole scan in `baseline_with_warning` mode;
- fail the governed run when policy/deployment configuration selects `fail_run`.

An audit row or metric alone is insufficient because a suppression rule failure can
silently reintroduce a known false positive.

### Safety/validator guard failure

Default behavior:

- fail closed for the affected governed action;
- preserve higher authority;
- escalate/review.

### Policy compile failure

- policy cannot activate;
- an already active immutable version remains active;
- run-specific invalid overlay is rejected.

### Plug-in load failure

- affected policy cannot activate if it references the missing plug-in;
- unrelated policies continue;
- no fallback to an action with the same unqualified name.

### Runtime budget exceeded

- stop behavior evaluation for the context;
- preserve baseline engine result;
- record timeout and policy/rule position;
- emit the same caller-visible degraded-run warning in `baseline_with_warning` mode;
- fail the governed run in `fail_run` mode;
- do not partially apply an un-reduced patch.

---

## PART 9 — IMPLEMENTATION RISKS

### Risk 1 — accidental generic mutation API

Avoid a public `set(path, value)` action. It becomes an unstable backdoor into domain
objects. Register explicit bounded actions.

### Risk 2 — policy and human overlay confusion

Keep scan-time policy and durable decisions separate. A reviewer demotion remains an
authoritative sidecar, not just another behavior rule.

### Risk 3 — duplicate application during migration

When compatibility conversion becomes active, ensure the legacy evaluator is shadow-only
or disabled. Never apply both.

### Risk 4 — weak fact versioning

Changing fact meaning can silently change policies. Descriptor stability and semantic
changes require a new fact ID or policy API version.

### Risk 5 — audit volume

Per-rule traces can be large. Keep full traces in run artifacts and emit summarized
operational events where appropriate, but do not lose applied/blocked provenance.

### Risk 6 — regex safety assumptions

Length limits do not prevent catastrophic matching. Use explicit execution protection.

### Risk 7 — plug-in trust ambiguity

Installed plug-ins are deployment code, not safe user scripts. Document this clearly
and keep them disabled unless allow-listed.

### Risk 8 — misleading accuracy claims

Human-reviewed samples may be biased. Report measured confirmation/reversal and labelled
coverage before claiming reduced FP/FN globally.

---

## PART 10 — DEFINITION OF DONE FOR THE FEATURE

The generalized feature is complete when:

1. Users can author a safe behavior policy without changing engine code.
2. Policy validates structurally and semantically before activation.
3. Simulation shows impact without writes.
4. Every active policy is immutable, versioned, approved, and content-addressed.
5. PII edge-rule behavior has verified parity through the new runtime.
6. Every applied or blocked operation has a structured, sanitized audit event.
7. Validator, human, safety, and LLM precedence tests pass.
8. Behavior-off mode is backward-compatible.
9. Contract writes still use only `ContractStore.upsert()`.
10. Trusted plug-ins can register actions without Redibis core edits.
11. Inline Python is not accepted by product policy APIs.
12. Rollback is immediate and does not delete history.
13. Runtime performance is bounded and observable.
14. The full test suite is green.
15. Public rules expose only `when`, `effects`, `reason`, `priority`, and `terminal`
    plus stable identity.
16. Linting and effective-order/shadow traces gate activation before the visual editor.
17. The PII MVP supports both post-verdict FP corrections and bounded pre-verdict FN
    threshold tuning.
18. Every rule has a mandatory non-empty rationale carried into simulation and audit.
19. Baseline fallback on evaluator failure always produces a caller-visible degraded-run
    warning; it is never observable only through telemetry.

---

## PART 11 — FIRST IMPLEMENTATION SLICE

For the smallest useful pull request, implement only:

1. Phase 0 validation/parity fixes.
2. `redibis/behavior/models.py`.
3. `redibis/behavior/schema.py`.
4. canonical compiler/SHA.
5. lint finding model plus structural/order lint checks.
6. synthetic unit tests.

Do not wire production engine behavior in the first slice.

The second slice should add registries, compile-time descriptor binding, registry-aware
lint checks, evaluator, reducer, and synthetic tests.

The third slice should add shadow audit/config.

The fourth slice should add PII conversion/parity, overlay consultation, and the bounded
pre-verdict threshold effect; only then switch authority.

This ordering keeps each change reviewable and protects the existing manual UI and
deterministic engine behavior.

