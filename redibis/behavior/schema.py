"""
redibis.behavior.schema
========================
Pydantic v2 schema models for behavior policy documents.

Structural validation happens here before compilation.
- Unknown keys are rejected (extra="forbid").
- Hard size/depth/count limits protect against DoS.
- reason is mandatory, non-empty, and bounded.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Hard limits (configurable per deployment; these are defaults)
# ---------------------------------------------------------------------------

MAX_DOCUMENT_BYTES   = 256 * 1024   # 256 KB per policy document
MAX_RULES_PER_POLICY = 200
MAX_CONDITION_DEPTH  = 10
MAX_CONDITION_NODES  = 500
MAX_EFFECTS_PER_RULE = 20
MAX_REASON_LEN       = 1024
MAX_ID_LEN           = 128
MAX_STRING_LEN       = 512
MAX_LIST_LEN         = 50
MAX_REGEX_LEN        = 256


# ---------------------------------------------------------------------------
# Condition schema
# ---------------------------------------------------------------------------

class PredicateSchema(BaseModel):
    """Atomic predicate: fact op value."""
    model_config = {"extra": "forbid"}

    fact:     str = Field(..., min_length=1, max_length=MAX_STRING_LEN)
    op:       str = Field(..., min_length=1, max_length=64)
    value:    Any


def _count_condition_nodes(node: Any, depth: int = 0) -> tuple[int, int]:
    """Return (node_count, max_depth) for a raw condition dict/object."""
    if depth > MAX_CONDITION_DEPTH:
        return (1, depth)
    if isinstance(node, dict):
        kind = node.get("all") or node.get("any") or node.get("not")
        if kind is not None:
            children = kind if isinstance(kind, list) else [kind]
            count, max_d = 1, depth
            for child in children:
                c, d = _count_condition_nodes(child, depth + 1)
                count += c
                max_d = max(max_d, d)
            return count, max_d
    return (1, depth)


# Use a Union with discriminator logic — Pydantic v2 forward-ref approach
ConditionNodeSchema = Union["ConditionGroupSchema", PredicateSchema]


class ConditionGroupSchema(BaseModel):
    """Boolean combinator: all/any/not over child conditions."""
    model_config = {"extra": "forbid"}

    all: Optional[list[ConditionNodeSchema]] = None
    any: Optional[list[ConditionNodeSchema]] = None
    not_: Optional[ConditionNodeSchema] = Field(None, alias="not")

    @model_validator(mode="after")
    def exactly_one_combinator(self) -> "ConditionGroupSchema":
        active = sum(1 for v in (self.all, self.any, self.not_) if v is not None)
        if active != 1:
            raise ValueError("condition group must have exactly one of: all, any, not")
        return self


ConditionGroupSchema.model_rebuild()


class EffectCallSchema(BaseModel):
    """Effect + params."""
    model_config = {"extra": "forbid"}

    effect: str = Field(..., min_length=1, max_length=MAX_STRING_LEN)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def params_bounded(cls, v: dict) -> dict:
        if len(v) > MAX_LIST_LEN:
            raise ValueError(f"params map exceeds {MAX_LIST_LEN} entries")
        return v


class RuleSchema(BaseModel):
    """Public v1 rule: id, when, effects, reason, priority, terminal."""
    model_config = {"extra": "forbid"}

    id:       str   = Field(..., min_length=1, max_length=MAX_ID_LEN)
    when:     ConditionNodeSchema
    effects:  list[EffectCallSchema] = Field(..., min_length=1)
    reason:   str   = Field(..., min_length=1, max_length=MAX_REASON_LEN,
                            description="Mandatory non-empty rationale for this rule.")
    priority: int   = Field(default=100)
    terminal: bool  = False

    @field_validator("id")
    @classmethod
    def id_path_safe(cls, v: str) -> str:
        from redibis.behavior.ids import validate_rule_id

        return validate_rule_id(v)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must be a non-empty, non-whitespace string")
        return v

    @field_validator("effects")
    @classmethod
    def effects_bounded(cls, v: list) -> list:
        if len(v) > MAX_EFFECTS_PER_RULE:
            raise ValueError(f"effects exceed maximum per rule ({MAX_EFFECTS_PER_RULE})")
        return v


class AppliesToSchema(BaseModel):
    """Policy-level scope: engine, stage, and optional filters."""
    model_config = {"extra": "forbid"}

    engine:       str = Field(..., min_length=1, max_length=64)
    stage:        str = Field(..., min_length=1, max_length=64)
    environments: list[str] = Field(default_factory=list)
    tables:       list[str] = Field(default_factory=list)
    columns:      list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    tenant:       Optional[str] = None
    effective_from:  Optional[str] = None
    effective_until: Optional[str] = None


class PolicyMetadataSchema(BaseModel):
    """Metadata block in a policy document."""
    model_config = {"extra": "forbid"}

    id:          str = Field(..., min_length=1, max_length=MAX_ID_LEN)
    version:     str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=MAX_STRING_LEN)
    owner:       str = Field(default="", max_length=MAX_STRING_LEN)

    @field_validator("id")
    @classmethod
    def id_path_safe(cls, v: str) -> str:
        from redibis.behavior.ids import validate_policy_id

        return validate_policy_id(v)

    @field_validator("version")
    @classmethod
    def version_path_safe(cls, v: str) -> str:
        from redibis.behavior.ids import validate_policy_version

        return validate_policy_version(v)


class BehaviorPolicyDocument(BaseModel):
    """
    Top-level behavior policy document.

    apiVersion: redibis.io/behavior-policy/v1
    kind: BehaviorPolicy
    metadata: ...
    appliesTo: ...
    rules: [...]
    """
    model_config = {"extra": "forbid"}

    apiVersion: Literal["redibis.io/behavior-policy/v1"]
    kind:       Literal["BehaviorPolicy"]
    metadata:   PolicyMetadataSchema
    appliesTo:  AppliesToSchema
    rules:      list[RuleSchema] = Field(..., min_length=1)

    @field_validator("rules")
    @classmethod
    def rules_bounded_and_unique(cls, v: list) -> list:
        if len(v) > MAX_RULES_PER_POLICY:
            raise ValueError(f"rules exceed maximum per policy ({MAX_RULES_PER_POLICY})")
        seen: set[str] = set()
        for rule in v:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id: {rule.id!r}")
            seen.add(rule.id)
        return v


def validate_document(raw: dict) -> BehaviorPolicyDocument:
    """
    Parse and validate a raw policy dict.

    Raises pydantic.ValidationError with path-specific messages on failure.
    """
    return BehaviorPolicyDocument.model_validate(raw)


def validate_document_size(raw_bytes: bytes | str) -> None:
    """Reject documents exceeding MAX_DOCUMENT_BYTES."""
    if isinstance(raw_bytes, str):
        raw_bytes = raw_bytes.encode()
    if len(raw_bytes) > MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"policy document exceeds maximum size "
            f"({len(raw_bytes)} > {MAX_DOCUMENT_BYTES} bytes)"
        )


def canonical_sha256(doc: "BehaviorPolicyDocument") -> str:
    """
    Produce a stable SHA-256 from the normalized document.

    YAML formatting and key-order changes do not change the digest.
    Semantic changes do change the digest.
    """
    # Use model_dump with sorted keys and stable serialization
    raw = doc.model_dump(mode="json", by_alias=True)
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
