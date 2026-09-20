# PII Guard + Rule Unification — Multi-Plan Handover

**Status:** Plans A–D landed (RuleSet from pack, ColumnScanner + `detect_pii` shim,
shared span/column `DeidApplier`, standalone `services/pii_guard` FastAPI service).
**Audience:** several coding LLMs/engineers, each taking ONE plan below with no shared
conversation context. Read §1–§3 first (shared context + naming), then your assigned plan.
**Related:** `docs/REDIBIS_PACK_DESIGN.md`, `docs/TEXT_PII_SCAN_DESIGN.md`,
`docs/BEHAVIOR_POLICY_ARCHITECTURE.md`, `docs/LOCALE_PACK_DESIGN.md`, `CLAUDE.md`.

---

## 1. North star (why this exists)

Today PII rules are **hardcoded in Python and split across two code paths**:

- the regex `CATALOG` (data, but living in `regex_catalog.py`);
- Arabic/telecom `context_hints` inlined in catalog entries;
- `DEFAULT_NER_LABELS` and the GLiNER phrase map in `ner_backend.py`;
- telecom special rules in `telecom_signals.py` + `phone_engine.py`;
- the **column** path (`detect_pii` → `equations`) and the planned **text** path
  (`text_scan`) each re-derive detection separately.

Two problems: (1) a customer can't change rules without editing code, and (2) column and
text scanning can drift because they don't share one rule source or one applier.

**Target:** rules are **data shipped in a pack**; one thin execution core applies them to
**both** column and text inputs; an external **PII Guard** service composes the scan core
and the mask core behind a stable API.

```
        ┌─────────────────────── Redibis Pack (data only) ───────────────────────┐
        │  RuleSet: regex patterns · context tokens · NER labels/phrases ·        │
        │           special/telecom rules · thresholds · deid policy              │
        └───────────────────────────────────┬────────────────────────────────────┘
                                             │ compiled once
                                             ▼
                                  RuleSet (immutable, in-memory)
                                             │
                   ┌─────────────────────────┼─────────────────────────┐
                   ▼                         ▼                         ▼
            ColumnScanner              TextScanner              (future scanners)
                   └─────────────┬───────────┘
                                 ▼
                         DetectionResult (spans OR column verdicts — one shape)
                                 ▼
                          MaskEngine (deid policy from the same pack)
                                 ▲
                                 │ composed by
        ┌────────────────────────┴───────────────────────────┐
        │   PII Guard service  (external, REST)               │
        │   /scan · /deidentify · /scan-and-mask · /policies  │
        │   scan backend  +  mask backend  behind one API     │
        └─────────────────────────────────────────────────────┘
```

## 2. Design law (applies to every plan)

1. **Rules are data; engines are code.** No detection/masking constant may be a Python
   literal that a customer would need to change. If it varies by deployment/locale, it
   lives in a pack section and is loaded into a `RuleSet`. (Same boundary as
   `parse_ge_rules`: packs are data, never executed.)
2. **One `RuleSet`, two scanners.** `ColumnScanner` and `TextScanner` MUST consume the
   same compiled `RuleSet` and the same recognizer implementations. A rule added to the
   pack changes both with zero extra code. A parity test enforces this.
3. **Detector produces evidence; a resolver produces verdicts.** Unchanged from
   `CLAUDE.md` #6. Recognizers emit candidates; a `SpanResolver`/equation decides.
4. **Mask never writes contracts; keys are per-run.** `CLAUDE.md` #11 holds everywhere,
   including the PII Guard service.
5. **Thin OOP, no god objects.** Each class does one thing and is named for it (§3).
   Prefer composition and small dataclasses over inheritance trees. No `Manager`,
   `Helper`, `Util`, `Base*` unless it is a real ABC with ≥2 implementations.
6. **Services depend inward.** `pii_guard` (service) → `redibis.pii` / `redibis.masking`
   (library). The library never imports the service. Same one-way rule as the rest of
   the repo.

## 3. Naming conventions (enforced — a handover LLM MUST follow these)

Consistent names are what let independent plans compose. Use exactly these.

### 3.1 Modules

```
redibis/pii/rules/                # NEW — pack-driven rule model + compiler
  ruleset.py        # RuleSet, RuleSetCompiler, RuleSource
  recognizers.py    # Recognizer protocol + built-ins (regex/phone/ner)
  resolver.py       # SpanResolver, VerdictResolver
redibis/pii/scan/                 # NEW — unified scan surface
  column_scanner.py # ColumnScanner
  text_scanner.py   # TextScanner
  result.py         # Detection, DetectionResult, DetectionKind
redibis/pii/deid/                 # NEW — span-level de-id over redibis.masking
  policy.py         # DeidPolicy, EntityRule
  applier.py        # DeidApplier
services/pii_guard/               # NEW — external service (own dist; see Plan D)
  app.py routes.py scan_backend.py mask_backend.py client.py
```

Legacy modules (`detector.py`, `equations.py`, `telecom_signals.py`, `ner_backend.py`)
are **refactored to delegate** into the above, not deleted wholesale — see Plan A.

### 3.2 Classes (names are the contract between plans)

| Class | Responsibility | Never does |
|---|---|---|
| `RuleSet` | immutable compiled rules (patterns, tokens, labels, phrases, special rules, thresholds) | I/O, mutation |
| `RuleSetCompiler` | pack sections → `RuleSet`; validates references | run detection |
| `Recognizer` (Protocol) | `recognize(text_or_values, ctx) -> list[Candidate]` | decide verdicts |
| `RegexRecognizer` / `PhoneRecognizer` / `NerRecognizer` | one evidence source each | know about other recognizers |
| `Candidate` (dataclass) | one raw hit: span/offsets or column stat + score + engine | be a verdict |
| `SpanResolver` | overlap merge + score floor → final spans (text) | mask |
| `VerdictResolver` | column candidates → column verdict (wraps current equation modes) | mask |
| `Detection` / `DetectionResult` | unified output; `kind` = `span` \| `column` | persist |
| `ColumnScanner` | scan a DataFrame → `DetectionResult(kind=column)` | text |
| `TextScanner` | scan a string → `DetectionResult(kind=span)` | columns |
| `DeidPolicy` / `EntityRule` | declarative "what to do per entity" | apply |
| `DeidApplier` | apply policy to spans via `MaskingEngine` | detect |
| `PiiGuardService` | compose scan + mask behind one API | own crypto/rules |
| `ScanBackend` / `MaskBackend` | service-side adapters over the library | HTTP details |

### 3.3 Rules for names

- Verbs for actions (`compile`, `recognize`, `resolve`, `apply`, `scan`), nouns for data
  (`RuleSet`, `Candidate`, `Detection`).
- No `Base` prefix unless it is an ABC with ≥2 concrete subclasses in-tree.
- One public class per module file where practical; module name = snake_case of the class.
- Suffix `Recognizer` = evidence source, `Resolver` = verdict maker, `Scanner` = input
  adapter, `Applier` = mutation, `Backend` = service adapter, `Service` = API composition.
- Dataclasses are `frozen=True` in the core; no mutable public fields.

## 4. The unified data model (shared by all plans)

```python
DetectionKind = Literal["span", "column"]

@dataclass(frozen=True)
class Candidate:
    entity_type: str
    score: float
    engine: str                     # "regex" | "phone" | "ner" | "llm"
    start: int | None = None        # set for text; None for column
    end: int | None = None
    text: str | None = None
    recognizer: str = ""
    validator: str = ""
    context_boost: bool = False

@dataclass(frozen=True)
class Detection:
    entity_type: str
    score: float
    engine: str
    start: int | None = None
    end: int | None = None
    text: str | None = None
    detected: bool = True           # verdict (column path may set False)
    evidence: tuple[Candidate, ...] = ()

@dataclass(frozen=True)
class DetectionResult:
    kind: DetectionKind
    detections: tuple[Detection, ...]
    entity_counts: Mapping[str, int]
    ruleset_id: str
    ruleset_version: str
    language: str
```

`Detection` unifies "a span in text" and "a column verdict": text detections carry
offsets; column detections carry `detected`/aggregate score. Both scanners return
`DetectionResult`. This is the seam that lets one mask/guard layer serve both.

---

# PLANS (each independently shippable; assign one LLM per plan)

Dependency order: **A → B → C** can proceed largely in parallel after A lands the
`RuleSet`; **D** depends on B + C. Each plan lists Context / Goal / Achieve / Tests / DoD.

## Plan A — Rule unification (the "clean out of code" plan)

**Context.** Rules live in `regex_catalog.py`, `ner_backend.py`, `telecom_signals.py`,
`phone_engine.py`, and inline `context_hints`. Pack format exists (`REDIBIS_PACK_DESIGN`).

**Goal.** One `RuleSet` compiled from pack sections, consumed everywhere. Legacy modules
keep working by delegating to it.

**Achieve.** ✅
- `RuleSetCompiler.compile_patterns` is the canonical catalog compile; `build_effective_catalog`
  delegates to it. Column `_run_presidio` delegates to `RegexRecognizer.recognize_values`.
- Pack sections compile into `RuleSet`; text, column, and PII Guard consume the same surface.

**Tests.** ✅ Parity corpus unchanged; pack override changes rules with no code edit;
cross-plan gate in `tests/test_cross_plan_ruleset_parity.py`.

**DoD.** ✅ `RuleSet` is the single source of rules; legacy modules delegate; corpus green.

## Plan B — Unified scan core (column + text share one applier)

**Context.** `detect_pii` (column) and the planned `text_scan` are separate. Recognizers
overlap conceptually but not in code.

**Goal.** `ColumnScanner` and `TextScanner` both consume one `RuleSet` + shared
`Recognizer` implementations, returning `DetectionResult`.

**Achieve.** ✅
- `ColumnScanner` + `VerdictResolver`; `detect_pii()` → `ColumnScanner.detect`.
- Column regex flows through `RegexRecognizer.recognize_values` (shared with text
  `RegexRecognizer.recognize`); pack `RuleSet` supplies overrides, tokens, region.
- `NerRecognizer.recognize_values` for column NER reducer surface.

**Tests.** ✅ Column parity corpus; text spans; pack rule in column + text + pii_guard
(`test_cross_plan_ruleset_parity.py`).

**DoD.** ✅ One rule source, two scanners, one result shape; `detect_pii` delegates.

## Plan C — De-identification over the unified result

**Context.** `redibis.masking` has strategies/engine/keys. `TEXT_PII_SCAN_DESIGN` §4
specced span-level de-id.

**Goal.** `DeidApplier` applies a `DeidPolicy` to any `DetectionResult` (span or column)
via `MaskingEngine`, with per-run keys.

**Achieve.** ✅
- `DeidApplier.apply(source, result, policy)` dispatches on `result.kind`: spans
  (right-to-left splice) or columns (per-cell via the same `_transform_span` /
  `MaskingEngine` primitives). `DeidResult.kind` + optional `deidentified_frame`.
- Fail-closed with no policy; keys never in `to_dict()`; `only_detected` gate for
  column evidence-only rows. `ColumnScanner.scan_and_deidentify` convenience path.
- Policy load / suggest unchanged (`DeidPolicy`, `suggest_policy_from_detections`).

**Tests.** ✅ Each strategy on a span; override beats default; fail-closed; keys never
in output; text de-id == column de-id for the same value + keys; column redact /
`only_detected`.

**DoD.** ✅ One applier for both kinds; policy is pack data; keys per-run, never leaked.

## Plan D — PII Guard service (external, composes scan + mask)

**Context.** Plans B+C give library scan + de-id. Customers want a network service that
does both behind one authenticated API (like the codegen split, vendor-hostable or
on-prem). Should be splittable into scan-backend and mask-backend later.

**Goal.** `services/pii_guard/` — a FastAPI service exposing scan, deidentify, and a
combined scan-and-mask, backed by the library, deployable standalone.

**Achieve.** ✅
- Package `redibis-pii-guard` under `services/pii_guard/` with `PiiGuardService`,
  `ScanBackend`, `MaskBackend`, separate `scan_routes` / `mask_routes`, vendor-token
  auth (`REDIBIS_PII_GUARD_API_KEY`), pack runtime + mtime hot-reload, Dockerfile,
  client, README.
- Routes: `POST /scan`, `/deidentify`, `/scan-and-mask`; `GET /policies`, `/ruleset`,
  `/health`. Metadata-only logs; fail-closed de-id; keys never in responses.

**Tests.** ✅ Contract tests in `tests/test_pii_guard_api.py` (auth, seam AST checks,
scan-and-mask == scan then deidentify, no payload in logs, pack reload).

**DoD.** ✅ One service, three write endpoints + discovery, pack-driven, split-ready.

## 5. Should you split scan and mask into two services?

**Recommendation: build PII Guard as one service now, with the `ScanBackend`/`MaskBackend`
seam, and split only if a concrete benefit appears.**

| Split when | Keep merged when (default) |
|---|---|
| mask must run in a stricter trust/residency zone than scan | one deployment, one auth, one pack — simpler ops |
| scan (NER/GPU) and mask (crypto/CPU) need independent scaling | latency: scan-and-mask in one hop, no inter-service round-trip |
| different teams/SLAs own detection vs de-identification | keys stay in one process; smaller attack surface |
| you sell "detection only" and "masking only" as separate SKUs | fewer moving parts to certify for compliance |

Because Plan D keeps them as two backend classes behind two route modules, splitting later
is a packaging change, not a rewrite. Don't pay the distributed-systems tax before a
requirement forces it.

## 6. Multi-LLM handover checklist

Give each LLM: this doc (§1–§4 + their plan), `CLAUDE.md`, `REDIBIS_PACK_DESIGN.md`, and
the one design doc their plan references. Require of each:

- follow §3 naming exactly (the names are the integration contract);
- land behind the pack — no new hardcoded rule constants;
- ship the parity test named in their DoD;
- `pytest tests/` green with their change in isolation;
- no cross-plan import that §3's module map doesn't allow.

Integration owner runs the cross-plan parity test (a pack rule visible in column scan,
text scan, and pii_guard simultaneously) as the final gate.

## 7. Risks

| Risk | Mitigation |
|---|---|
| "Clean from OOP" over-corrects into a hardcoded rewrite | law #1/#5: rules become data, engines stay small typed classes — not a functional rewrite, not a god object |
| Column parity regresses during unification | Plan A/B parity corpus is the gate; delegate, don't rewrite, the equation path |
| Two scanners drift again | law #2 + the shared-`RuleSet` parity test in Plan B |
| Premature scan/mask split | §5: one service, documented seam, split on evidence |
| Naming drift across LLMs | §3 is normative; integration owner rejects PRs that rename contract classes |
| Keys leak through the new service | law #4; metadata-only logging test in Plan D |
