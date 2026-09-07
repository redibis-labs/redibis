"""
redibis.behavior.adapters.profiling
=====================================
Profiling triage behavior adapter.

Responsibilities
----------------
1. Build a BehaviorContext from a column profile or table-level metadata.
2. Declare profiling-specific capabilities per hook stage.
3. Return a ProfilingDirective dataclass — never mutate profiler result dicts.

ProfilingDirective
------------------
The only output of this adapter is a ``ProfilingDirective``. Callers must
respect it; they may not arbitrarily mutate profiler result dicts.

Safety invariants
-----------------
- Profiler result dicts are NEVER mutated by this adapter.
- Only profilers from the registered ``PROFILER_REGISTRY`` may be selected.
- Triage threshold adjustments are bounded by hard limits.
- No raw cell values in BehaviorContext.
- All patch operations are run-scoped and do not persist to config files.

Feature gating
--------------
Check ``behavior.engine_modes.get("profiling")`` or call
``engine_enabled(cfg, "profiling")`` before invoking any function here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import (
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    HookStage,
    JSONValue,
    RuntimeMode,
)

# Hard bounds for triage threshold adjustment
_TRIAGE_THRESHOLD_MIN = 0.0
_TRIAGE_THRESHOLD_MAX = 1.0

# Registered profiler names allowed for selection by policy
_ALLOWED_PROFILERS = frozenset({
    "great_expectations",
    "duckdb",
    "openmetadata",
    "ydata",
})


# ---------------------------------------------------------------------------
# Feature-gate helper
# ---------------------------------------------------------------------------

def engine_enabled(cfg: BehaviorConfig, engine: str = "profiling") -> bool:
    """Return True if the profiling adapter is active or shadow."""
    mode = cfg.effective_mode(engine)
    return mode in (RuntimeMode.SHADOW, RuntimeMode.ACTIVE)


# ---------------------------------------------------------------------------
# Directive model
# ---------------------------------------------------------------------------

@dataclass
class ProfilingDirective:
    """
    Run-scoped directive returned by the profiling adapter.

    Callers consult this before starting a profiling pass. Fields are additive:
    if ``require_deep_profile`` is True, a deep profiling pass is requested
    in addition to the standard pass.

    ``selected_profiler`` must be a key from ``redibis.profiling.PROFILER_REGISTRY``
    (or None to use the configured default).

    ``triage_threshold`` is run-scoped and overrides the config value only for
    this scan if it falls within [_TRIAGE_THRESHOLD_MIN, _TRIAGE_THRESHOLD_MAX].
    """
    require_deep_profile: bool = False
    selected_profiler: Optional[str] = None
    triage_threshold: Optional[float] = None
    require_review: bool = False
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def build_profiling_context(
    table: str,
    *,
    column: Optional[str] = None,
    column_profile: Any = None,
    run_id: str = "",
    stage: HookStage = HookStage.PRE_VERDICT,
    registry_manifest_sha256: str = "",
    environment: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    current_profiler: Optional[str] = None,
    row_count: Optional[int] = None,
    column_count: Optional[int] = None,
) -> BehaviorContext:
    """
    Build a BehaviorContext for profiling triage decisions.

    Only aggregate metadata is included — no raw values.
    """
    facts: dict[str, JSONValue] = {
        "table.name": table,
    }

    if column:
        facts["column.name"] = column
    if current_profiler:
        facts["profiler.current"] = current_profiler
    if row_count is not None:
        facts["table.row_count"] = int(row_count)
    if column_count is not None:
        facts["table.column_count"] = int(column_count)

    if column_profile is not None:
        null_rate = getattr(column_profile, "null_rate", None)
        if null_rate is not None:
            facts["profile.null_rate"] = float(null_rate)
        cardinality = getattr(column_profile, "cardinality_ratio", None)
        if cardinality is not None:
            facts["profile.cardinality_ratio"] = float(cardinality)
        logical_type = getattr(column_profile, "logical_type", None)
        if logical_type:
            facts["column.logical_type"] = str(logical_type)
        triage_score = getattr(column_profile, "triage_score", None)
        if triage_score is not None:
            facts["profile.triage_score"] = float(triage_score)

    caps = _profiling_capabilities(stage)
    return BehaviorContext(
        run_id=run_id,
        table=table,
        column=column,
        engine="profiling",
        stage=stage,
        facts=facts,
        capabilities=caps,
        registry_manifest_sha256=registry_manifest_sha256,
        environment=environment,
        jurisdiction=jurisdiction,
    )


# ---------------------------------------------------------------------------
# Capabilities declaration
# ---------------------------------------------------------------------------

def _profiling_capabilities(stage: HookStage) -> BehaviorCapabilities:
    if stage in (HookStage.PRE_VERDICT, HookStage.PRE_EVIDENCE):
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "table.name", "column.name",
                "profiler.current",
                "table.row_count", "table.column_count",
                "profile.null_rate", "profile.cardinality_ratio",
                "column.logical_type", "profile.triage_score",
            }),
            allowed_actions=frozenset({
                "profiling.require_deep_profile",
                "profiling.select_profiler",
                "profiling.adjust_triage_threshold",
                "core.review.require",
                "core.review.add_reason",
            }),
            allowed_patch_operations=frozenset({
                "profiling.require_deep_profile",
                "profiling.select_profiler",
                "profiling.triage_threshold",
                "review.require",
                "review.add_reason",
            }),
            immutable_paths=frozenset(),
            value_constraints={
                "profiling.select_profiler": {
                    "enum": sorted(_ALLOWED_PROFILERS),
                },
                "profiling.triage_threshold": {
                    "minimum": _TRIAGE_THRESHOLD_MIN,
                    "maximum": _TRIAGE_THRESHOLD_MAX,
                },
            },
        )
    return BehaviorCapabilities()


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def apply_patch_to_directive(
    patch: BehaviorPatch,
    *,
    rule_id: str = "",
    policy_id: str = "",
) -> ProfilingDirective:
    """
    Interpret a BehaviorPatch as a ProfilingDirective.

    Does not touch any profiler result dict. The returned directive is
    handed to the pipeline caller to act on before or around the profiling pass.

    - ``profiling.require_deep_profile``:  sets require_deep_profile=True.
    - ``profiling.select_profiler``:  sets selected_profiler if in allowlist.
    - ``profiling.triage_threshold``:  sets triage_threshold within bounds.
    - ``review.require`` / ``review.add_reason``:  review flags.
    """
    require_deep = False
    selected_profiler: Optional[str] = None
    triage_threshold: Optional[float] = None
    require_review = patch.require_review
    reasons: list[str] = list(patch.reasons)

    for op in patch.operations:
        if op.op != "set":
            continue

        if op.path == "profiling.require_deep_profile":
            require_deep = bool(op.value)

        elif op.path == "profiling.select_profiler":
            profiler_name = str(op.value or "").lower()
            if profiler_name in _ALLOWED_PROFILERS:
                selected_profiler = profiler_name

        elif op.path == "profiling.triage_threshold":
            if isinstance(op.value, (int, float)):
                clamped = max(
                    _TRIAGE_THRESHOLD_MIN,
                    min(_TRIAGE_THRESHOLD_MAX, float(op.value)),
                )
                triage_threshold = clamped

        elif op.path == "review.require":
            require_review = True

        elif op.path == "review.add_reason" and op.value is not None:
            reasons.append(str(op.value)[:512])

    return ProfilingDirective(
        require_deep_profile=require_deep,
        selected_profiler=selected_profiler,
        triage_threshold=triage_threshold,
        require_review=require_review,
        reasons=reasons,
    )
