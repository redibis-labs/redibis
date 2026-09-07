"""
redibis.behavior.models
========================
Immutable frozen dataclasses for the Behavior Policy Runtime.

Design invariants
-----------------
- All public models are frozen dataclasses; no mutable state.
- JSON/YAML-safe: no callables, no framework objects, no raw cell values.
- AuthoredRule exposes only the public authoring surface:
    id, when, effects, reason, priority, terminal.
- CompiledRule adds compiler-derived enforcement fields that authors do not set.
- BehaviorContext never contains raw cell values.
- Patches are proposals; adapters validate and apply them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

# ---------------------------------------------------------------------------
# JSON-safe value type
# ---------------------------------------------------------------------------

JSONValue = Union[None, bool, int, float, str, list, dict]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class HookStage(str, enum.Enum):
    """Engine hook stages where policies may execute."""
    PRE_EVIDENCE  = "pre_evidence"
    POST_EVIDENCE = "post_evidence"
    PRE_VERDICT   = "pre_verdict"
    POST_VERDICT  = "post_verdict"
    PRE_ACTION    = "pre_action"
    POST_ACTION   = "post_action"


class TruthValue(str, enum.Enum):
    """Three-valued logic result for predicate evaluation."""
    TRUE    = "TRUE"
    FALSE   = "FALSE"
    UNKNOWN = "UNKNOWN"


class UnknownBehavior(str, enum.Enum):
    """What to do when a fact is unavailable. Fixed to NO_MATCH in v1."""
    NO_MATCH = "no_match"


class ConflictPolicy(str, enum.Enum):
    """How to handle incompatible scalar effects. Fixed to REVIEW in v1."""
    REVIEW = "review"


class Authority(int, enum.Enum):
    """Ranked authority levels; higher integer = higher rank."""
    SAFETY_GUARD          = 60
    AUTHORITATIVE_VALIDATOR = 50
    HUMAN_DECISION        = 40
    APPROVED_POLICY       = 30
    ENGINE_NATIVE         = 20
    LLM_PROPOSAL          = 10


class PolicySource(str, enum.Enum):
    """Origin of a policy document — determines derived authority."""
    BUILTIN     = "builtin"
    DEPLOYMENT  = "deployment"
    TENANT      = "tenant"
    USER        = "user"
    LLM_PROPOSED = "llm_proposed"


class FailMode(str, enum.Enum):
    """Behavior on evaluator failure."""
    BASELINE_WITH_WARNING = "baseline_with_warning"
    FAIL_RUN              = "fail_run"


class RuntimeMode(str, enum.Enum):
    """Global runtime activation mode."""
    DISABLED = "disabled"
    SHADOW   = "shadow"
    ACTIVE   = "active"


class RuleTraceStatus(str, enum.Enum):
    OUT_OF_SCOPE    = "out_of_scope"
    CONDITION_FALSE = "condition_false"
    UNKNOWN_NO_MATCH = "unknown_no_match"
    MATCHED         = "matched"
    EFFECT_ERROR    = "effect_error"
    TERMINATED      = "terminated"
    SHADOWED        = "shadowed"


class LintSeverity(str, enum.Enum):
    ERROR   = "error"    # blocks activation
    WARNING = "warning"  # requires simulation
    INFO    = "info"


# ---------------------------------------------------------------------------
# Scope and condition models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleScope:
    """Optional filters that restrict when a policy/rule applies."""
    engines:        tuple[str, ...] = ()
    tables:         tuple[str, ...] = ()
    columns:        tuple[str, ...] = ()
    environments:   tuple[str, ...] = ()
    jurisdictions:  tuple[str, ...] = ()
    tenant:         Optional[str]   = None
    effective_from: Optional[str]   = None
    effective_until: Optional[str]  = None


@dataclass(frozen=True)
class Predicate:
    """Atomic condition: fact op expected."""
    fact:     str
    operator: str
    expected: JSONValue


@dataclass(frozen=True)
class ConditionGroup:
    """Boolean combinator over nested ConditionNodes."""
    kind:     str                      # "all" | "any" | "not"
    children: tuple["ConditionNode", ...]


# ConditionNode is either a Predicate or a ConditionGroup.
ConditionNode = Union[Predicate, ConditionGroup]


@dataclass(frozen=True)
class EffectCall:
    """A registered effect identifier and its parameters."""
    effect: str
    params: dict[str, JSONValue] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Authored rule — public surface (what operators write)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthoredRule:
    """
    The public v1 rule model.

    Authors set only: id, when, effects, reason, priority, terminal.
    Engine, hook, scope, authority, conflict_policy, and on_unknown are
    derived by the compiler and live on CompiledRule.
    """
    id:       str
    when:     ConditionNode
    effects:  tuple[EffectCall, ...]
    reason:   str               # mandatory non-empty
    priority: int  = 100
    terminal: bool = False


# ---------------------------------------------------------------------------
# Compiled rule — internal enforcement model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompiledRule:
    """Internal model produced by the compiler. Authors do not set these fields."""
    id:             str
    stage:          HookStage
    priority:       int
    scope:          RuleScope
    condition:      ConditionNode
    effects:        tuple[EffectCall, ...]
    reason:         str
    terminal:       bool
    authority:      Authority
    on_unknown:     UnknownBehavior = UnknownBehavior.NO_MATCH   # fixed v1
    conflict_policy: ConflictPolicy = ConflictPolicy.REVIEW       # fixed v1
    source_layer:   str             = ""
    policy_id:      str             = ""
    policy_version: str             = ""


# ---------------------------------------------------------------------------
# Policy document models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BehaviorPolicy:
    """Compiled, immutable policy document."""
    id:            str
    version:       str
    description:   str
    engine:        str
    stage:         HookStage
    scope:         RuleScope
    rules:         tuple[AuthoredRule, ...]
    content_sha256: str
    source:        PolicySource
    owner:         str = ""


@dataclass(frozen=True)
class BehaviorPolicySet:
    """Effective set of compiled rules for one engine+stage evaluation."""
    compiled_rules:  tuple[CompiledRule, ...]
    policies:        tuple[BehaviorPolicy, ...]
    manifest_sha256: str  # digest of registry manifest + all policy SHAs


# ---------------------------------------------------------------------------
# Evaluation context — never contains raw values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BehaviorCapabilities:
    """What an adapter allows for a given engine+stage."""
    readable_facts:          frozenset[str] = field(default_factory=frozenset)
    allowed_actions:         frozenset[str] = field(default_factory=frozenset)
    allowed_patch_operations: frozenset[str] = field(default_factory=frozenset)
    immutable_paths:         frozenset[str] = field(default_factory=frozenset)
    value_constraints:       dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BehaviorContext:
    """
    Privacy-safe evaluation context.

    Contains column/table semantics, scores, rates, validator outcomes,
    types, identifiers. NEVER contains raw cell values.
    """
    run_id:                  str
    table:                   str
    engine:                  str
    stage:                   HookStage
    facts:                   Mapping[str, JSONValue]
    capabilities:            BehaviorCapabilities
    registry_manifest_sha256: str
    column:                  Optional[str] = None
    environment:             Optional[str] = None
    jurisdiction:            Optional[str] = None
    tenant:                  Optional[str] = None


# ---------------------------------------------------------------------------
# Patch models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PatchOperation:
    """A single typed change proposal."""
    op:        str         # e.g. "set", "add", "remove"
    path:      str         # e.g. "verdict.entity"
    value:     JSONValue
    authority: Authority
    reason:    str
    rule_id:   str         = ""
    policy_id: str         = ""


@dataclass(frozen=True)
class BehaviorPatch:
    """Accumulated patch from one or more rules."""
    operations:     tuple[PatchOperation, ...] = ()
    require_review: bool                        = False
    review_role:    Optional[str]               = None
    reasons:        tuple[str, ...]             = ()


# ---------------------------------------------------------------------------
# Trace / evaluation result models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleTrace:
    """Per-rule evaluation record."""
    rule_id:      str
    policy_id:    str
    priority:     int
    status:       RuleTraceStatus
    reason:       str
    patch:        Optional[BehaviorPatch] = None
    error:        Optional[str]          = None


@dataclass(frozen=True)
class BlockedOperation:
    """An operation that was proposed but rejected."""
    operation:    PatchOperation
    blocked_by:   str      # authority name or reason
    explanation:  str


@dataclass(frozen=True)
class EvaluationTrace:
    """Complete trace for one context evaluation."""
    policy_sha256:      str
    context_hash:       str
    evaluated_rules:    tuple[RuleTrace, ...]
    proposed_patch:     BehaviorPatch
    applied_patch:      BehaviorPatch
    blocked_operations: tuple[BlockedOperation, ...]
    duration_ms:        float = 0.0


# ---------------------------------------------------------------------------
# Lint models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LintFinding:
    """A structural or semantic lint warning/error for a policy."""
    finding_id:  str          # stable ID e.g. "SHADOWED_RULE"
    severity:    LintSeverity
    policy_id:   str
    rule_id:     Optional[str]
    message:     str
    suggestion:  str = ""


# ---------------------------------------------------------------------------
# Warning model for caller-visible degraded runs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BehaviorPolicyWarning:
    """
    Caller-visible warning emitted when baseline_with_warning fallback triggers.

    This is NEVER silent — it must reach library/service results, CLI/web
    progress, run artifacts, and structured audit.
    """
    policy_id:   Optional[str]
    rule_id:     Optional[str]
    engine:      str
    stage:       str
    target:      str          # table/column being evaluated
    fallback:    str          # description of what the baseline produced
    error:       str          # the error that triggered fallback


# ---------------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyAuditEvent:
    """Sanitized, structured audit event for a policy evaluation."""
    event_id:         str
    ts:               str
    run_id:           str
    table:            str
    column:           Optional[str]
    engine:           str
    stage:            str
    policy_id:        str
    policy_version:   str
    policy_sha256:    str
    rule_id:          str
    action_ids:       tuple[str, ...]
    actor:            str
    context_hash:     str
    before_hash:      str
    proposed_patch:   dict
    applied_patch:    dict
    blocked_operations: tuple[dict, ...]
    outcome:          str
    duration_ms:      float
