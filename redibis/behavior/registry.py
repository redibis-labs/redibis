"""
redibis.behavior.registry
==========================
Metadata-first registries for facts, operators, and effects.

Design
------
- Built-in IDs win on collisions with third-party IDs.
- Third-party IDs must be namespaced (contain ".").
- Registration is idempotent for identical descriptors.
- A registry manifest SHA identifies the exact set of registered capabilities.
- Implementations (callables) are never stored in descriptors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from redibis.behavior.models import (
    Authority,
    BehaviorContext,
    BehaviorPatch,
    EffectCall,
    HookStage,
    JSONValue,
)

# ---------------------------------------------------------------------------
# Descriptor dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FactDescriptor:
    """Metadata for a registered fact."""
    id:               str
    value_type:       str                    # "str" | "float" | "bool" | "list" | "dict"
    description:      str
    supported_engines: tuple[str, ...]
    supported_stages:  tuple[HookStage, ...]
    privacy_class:    str                    # "public" | "metadata" | "sensitive_aggregate"
    stability:        str                    # "stable" | "experimental"
    nullable:         bool = True


@dataclass(frozen=True)
class OperatorDescriptor:
    """Metadata for a registered condition operator."""
    id:             str
    description:    str
    input_types:    tuple[str, ...]   # value types this operator works on
    expected_types: tuple[str, ...]   # types the expected param can be


@dataclass(frozen=True)
class EffectDescriptor:
    """Metadata for a registered effect."""
    id:               str
    description:      str
    params_schema:    dict[str, Any]
    supported_engines: tuple[str, ...]
    supported_stages:  tuple[HookStage, ...]
    patch_operations:  tuple[str, ...]
    side_effect:      str   # "none" | "review" | "write" | "external"
    autonomy:         str   # "auto" | "suggest-only" | "gate"


# ---------------------------------------------------------------------------
# Action protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class BehaviorAction(Protocol):
    """Protocol that effect handler implementations must satisfy."""
    descriptor: EffectDescriptor

    def apply(
        self,
        context: BehaviorContext,
        params: Mapping[str, JSONValue],
    ) -> BehaviorPatch:
        """Return a deterministic patch; perform no mutation or I/O."""
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class BehaviorRegistry:
    """
    Thread-safe (read-only after build) registry of facts, operators, and effects.

    Built-in identifiers always win on collision.
    Third-party IDs must be namespaced (contain ".").
    """

    def __init__(self) -> None:
        self._facts:     dict[str, FactDescriptor]     = {}
        self._operators: dict[str, OperatorDescriptor] = {}
        self._effects:   dict[str, EffectDescriptor]   = {}
        self._actions:   dict[str, BehaviorAction]     = {}
        self._fact_providers: dict[str, Optional[Callable]] = {}
        self._builtin_facts:    frozenset[str]         = frozenset()
        self._builtin_ops:      frozenset[str]         = frozenset()
        self._builtin_effects:  frozenset[str]         = frozenset()
        self._manifest_sha256:  str                    = ""

    # ------------------------------------------------------------------
    # Fact registration

    def register_fact(
        self,
        desc: FactDescriptor,
        *,
        builtin: bool = False,
        allow_replace: bool = False,
        provider: Optional[Callable] = None,
    ) -> None:
        """Register a fact descriptor (and optional runtime provider)."""
        existing = self._facts.get(desc.id)
        if existing is not None:
            if desc.id in self._builtin_facts:
                raise ValueError(
                    f"Cannot replace built-in fact {desc.id!r} with a third-party descriptor"
                )
            if not allow_replace:
                raise ValueError(
                    f"Duplicate fact registration: {desc.id!r} — "
                    "use allow_replace=True to override"
                )
        if builtin:
            self._builtin_facts = self._builtin_facts | {desc.id}
        self._facts[desc.id] = desc
        if provider is not None:
            self._fact_providers[desc.id] = provider
        self._invalidate_manifest()

    # ------------------------------------------------------------------
    # Operator registration

    def register_operator(
        self,
        desc: OperatorDescriptor,
        *,
        builtin: bool = False,
        impl: Optional[Callable] = None,
    ) -> None:
        """Register an operator descriptor."""
        if desc.id in self._builtin_ops and not builtin:
            raise ValueError(f"Cannot replace built-in operator {desc.id!r}")
        if builtin:
            self._builtin_ops = self._builtin_ops | {desc.id}
        self._operators[desc.id] = desc
        self._invalidate_manifest()

    # ------------------------------------------------------------------
    # Effect registration

    def register_effect(
        self,
        desc: EffectDescriptor,
        *,
        builtin: bool = False,
        handler: Optional[BehaviorAction] = None,
    ) -> None:
        """Register an effect descriptor and optional handler."""
        if desc.id in self._builtin_effects and not builtin:
            raise ValueError(
                f"Cannot replace built-in effect {desc.id!r}"
            )
        if builtin:
            self._builtin_effects = self._builtin_effects | {desc.id}
        self._effects[desc.id] = desc
        if handler is not None:
            self._actions[desc.id] = handler
        self._invalidate_manifest()

    # ------------------------------------------------------------------
    # Lookup

    def get_fact(self, id: str) -> Optional[FactDescriptor]:
        return self._facts.get(id)

    def has_fact(self, id: str) -> bool:
        return id in self._facts

    def get_operator(self, id: str) -> Optional[OperatorDescriptor]:
        return self._operators.get(id)

    def has_operator(self, id: str) -> bool:
        return id in self._operators

    def get_effect(self, id: str) -> Optional[EffectDescriptor]:
        return self._effects.get(id)

    def has_effect(self, id: str) -> bool:
        return id in self._effects

    def get_action(self, id: str) -> Optional[BehaviorAction]:
        return self._actions.get(id)

    # ------------------------------------------------------------------
    # Catalogue

    def catalogue(self) -> dict:
        """Serializable catalogue for the structured editor / API."""
        return {
            "facts": {
                k: {
                    "value_type": v.value_type,
                    "description": v.description,
                    "supported_engines": list(v.supported_engines),
                    "supported_stages": [s.value for s in v.supported_stages],
                    "privacy_class": v.privacy_class,
                    "stability": v.stability,
                    "nullable": v.nullable,
                }
                for k, v in sorted(self._facts.items())
            },
            "operators": {
                k: {
                    "description": v.description,
                    "input_types": list(v.input_types),
                    "expected_types": list(v.expected_types),
                }
                for k, v in sorted(self._operators.items())
            },
            "effects": {
                k: {
                    "description": v.description,
                    "params_schema": v.params_schema,
                    "supported_engines": list(v.supported_engines),
                    "supported_stages": [s.value for s in v.supported_stages],
                    "patch_operations": list(v.patch_operations),
                    "side_effect": v.side_effect,
                    "autonomy": v.autonomy,
                }
                for k, v in sorted(self._effects.items())
            },
        }

    @property
    def manifest_sha256(self) -> str:
        """Stable digest of the current registry contents."""
        if not self._manifest_sha256:
            self._manifest_sha256 = self._compute_manifest()
        return self._manifest_sha256

    def _invalidate_manifest(self) -> None:
        self._manifest_sha256 = ""

    def _compute_manifest(self) -> str:
        data = json.dumps(self.catalogue(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Built-in registry (singleton)
# ---------------------------------------------------------------------------

_BUILTIN_REGISTRY: Optional[BehaviorRegistry] = None


def _build_builtin_registry() -> BehaviorRegistry:
    """Build and return the default built-in registry."""
    reg = BehaviorRegistry()

    # ── Built-in facts ─────────────────────────────────────────────────────
    pii_stages = (
        HookStage.POST_EVIDENCE, HookStage.PRE_VERDICT, HookStage.POST_VERDICT
    )

    def _fact(id, vtype, desc, stages=pii_stages, engines=("pii",),
              privacy="metadata", stability="stable", nullable=True):
        reg.register_fact(
            FactDescriptor(
                id=id, value_type=vtype, description=desc,
                supported_engines=tuple(engines),
                supported_stages=tuple(stages),
                privacy_class=privacy, stability=stability, nullable=nullable,
            ),
            builtin=True,
        )

    _fact("column.name",          "str",   "Column name (identifier only)")
    _fact("column.logical_type",  "str",   "Logical type (string, integer, float, …)")
    _fact("column.is_numeric",    "bool",  "Whether the column is numeric")
    _fact("column.is_integer",    "bool",  "Whether the column is integer type")
    _fact("profile.null_rate",    "float", "Fraction of null values [0,1]")
    _fact("profile.cardinality_ratio", "float", "Unique-value fraction [0,1]")
    _fact("evidence.regex.score", "float", "Presidio match confidence", nullable=True)
    _fact("evidence.regex.match_rate", "float", "Fraction of values matched by regex", nullable=True)
    _fact("evidence.ner.score",   "float", "GLiNER NER score", nullable=True)
    _fact("evidence.ner.match_rate", "float", "Fraction of values matched by NER", nullable=True)
    _fact("validators.phone.valid_rate", "float", "Phone/MSISDN valid fraction [0,1]", nullable=True)
    _fact("validators.luhn.status",  "str", "'pass' | 'fail' | null if not tested", nullable=True)
    _fact("validators.iccid.status", "str", "'pass' | 'fail' | null if not tested", nullable=True)
    _fact("validators.nid.status",   "str", "'pass' | 'fail' | null if not tested", nullable=True)
    _fact("verdict.detected",    "bool",  "Current PII verdict (true/false)")
    _fact("verdict.entity",      "str",   "Current canonical entity type", nullable=True)
    _fact("verdict.confidence",  "float", "Verdict confidence [0,1]")
    _fact("verdict.equation",    "str",   "Equation mode used for verdict")

    # ── Built-in operators ─────────────────────────────────────────────────
    def _op(id, desc, in_types, exp_types):
        reg.register_operator(
            OperatorDescriptor(id=id, description=desc,
                               input_types=tuple(in_types),
                               expected_types=tuple(exp_types)),
            builtin=True,
        )

    _op("eq",           "Equals",                   ["str","bool","int","float"], ["str","bool","int","float"])
    _op("neq",          "Not equals",               ["str","bool","int","float"], ["str","bool","int","float"])
    _op("lt",           "Less than",                ["int","float"], ["int","float"])
    _op("lte",          "Less than or equal",       ["int","float"], ["int","float"])
    _op("gt",           "Greater than",             ["int","float"], ["int","float"])
    _op("gte",          "Greater than or equal",    ["int","float"], ["int","float"])
    _op("between",      "Between lo and hi (inclusive)", ["int","float"], ["list"])
    _op("in",           "Value in set",             ["str","int","float","bool"], ["list"])
    _op("not_in",       "Value not in set",         ["str","int","float","bool"], ["list"])
    _op("contains",     "String contains substring",["str"], ["str"])
    _op("contains_any", "Contains any of list",     ["str","list"], ["list"])
    _op("contains_all", "Contains all of list",     ["str","list"], ["list"])
    _op("matches",      "Regex match (bounded)",    ["str"], ["str"])
    _op("starts_with",  "Starts with prefix",       ["str"], ["str"])
    _op("ends_with",    "Ends with suffix",         ["str"], ["str"])
    _op("exists",       "Fact exists and is not null", ["str","int","float","bool","list"], [])
    _op("is_null",      "Fact is null/missing",     ["str","int","float","bool","list"], [])
    _op("is_true",      "Boolean fact is true",     ["bool"], [])
    _op("is_false",     "Boolean fact is false",    ["bool"], [])

    # ── Built-in effects ───────────────────────────────────────────────────
    all_stages = tuple(HookStage)

    def _eff(id, desc, params_schema, ops, side_effect="none", autonomy="auto",
             stages=pii_stages, engines=("pii",)):
        reg.register_effect(
            EffectDescriptor(
                id=id, description=desc,
                params_schema=params_schema,
                supported_engines=tuple(engines),
                supported_stages=tuple(stages),
                patch_operations=tuple(ops),
                side_effect=side_effect,
                autonomy=autonomy,
            ),
            builtin=True,
        )

    _eff(
        "core.verdict.set_detected",
        "Set verdict.detected to a boolean value",
        {"detected": {"type": "boolean"}},
        ["verdict.set_detected"],
        stages=(HookStage.POST_VERDICT,),
    )
    _eff(
        "core.verdict.set_entity",
        "Set verdict.entity to a registered entity type",
        {"entity": {"type": "string"}},
        ["verdict.set_entity"],
        stages=(HookStage.POST_VERDICT,),
    )
    _eff(
        "core.verdict.set_confidence",
        "Set verdict.confidence to a bounded value",
        {"confidence": {"type": "number", "minimum": 0, "maximum": 1}},
        ["verdict.set_confidence"],
        stages=(HookStage.POST_VERDICT,),
    )
    _eff(
        "core.review.require",
        "Mark the verdict as requiring human review",
        {"role": {"type": "string", "default": "steward"}},
        ["review.require"],
        side_effect="review",
        autonomy="gate",
    )
    _eff(
        "core.review.add_reason",
        "Add a supplemental review reason (does not satisfy rule-level reason)",
        {"reason": {"type": "string", "minLength": 1}},
        ["review.add_reason"],
        side_effect="review",
        autonomy="auto",
    )
    _eff(
        "core.threshold.set_for_run",
        "Adjust a PII threshold for the current run only (bounded by adapter)",
        {
            "threshold": {"type": "string"},
            "value": {"type": "number", "minimum": 0, "maximum": 1},
        },
        ["threshold.set_for_run"],
        stages=(HookStage.PRE_VERDICT,),
        side_effect="none",
        autonomy="suggest-only",
    )

    return reg


def get_builtin_registry() -> BehaviorRegistry:
    """Return the singleton built-in registry, building it on first call."""
    global _BUILTIN_REGISTRY
    if _BUILTIN_REGISTRY is None:
        _BUILTIN_REGISTRY = _build_builtin_registry()
    return _BUILTIN_REGISTRY
