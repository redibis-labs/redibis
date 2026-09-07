"""Dataclasses for the multi-domain classification policy engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class TagRef:
    """A tag within a domain, e.g. DataSensitivity:PII."""

    domain: str
    tag: str

    def key(self) -> str:
        return f"{self.domain}:{self.tag}"

    @classmethod
    def from_key(cls, key: str) -> "TagRef":
        domain, tag = key.split(":", 1)
        return cls(domain=domain, tag=tag)


@dataclass
class CandidateTag:
    """Evidence producer output — a proposed tag with confidence and provenance."""

    domain: str
    tag: str
    confidence: float = 0.0
    source: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    suggest_only: bool = False

    def ref(self) -> TagRef:
        return TagRef(domain=self.domain, tag=self.tag)


@dataclass
class ResolvedTag:
    """A tag after deterministic policy resolution."""

    domain: str
    tag: str
    attributes: dict[str, Any] = field(default_factory=dict)
    propagate: bool = True
    derived: bool = False
    source: str = ""

    def ref(self) -> TagRef:
        return TagRef(domain=self.domain, tag=self.tag)

    def to_atlas(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"typeName": self.tag}
        if self.attributes:
            payload["attributes"] = dict(self.attributes)
        if not self.propagate:
            payload["propagate"] = False
        return payload


@dataclass
class ClassificationContext:
    """Table/column context fed into the policy engine."""

    table: str
    column: str = ""
    jurisdiction: str = ""
    lifecycle: str = ""
    privacy_state: str = ""
    column_logical_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassificationResult:
    """Deterministic output of the policy engine for one column or table."""

    table: str
    column: str = ""
    resolved_tags: list[ResolvedTag] = field(default_factory=list)
    retention_days: Optional[int] = None
    retention_tag: str = ""
    approval_role: str = ""
    escalations: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    candidates_dropped: list[str] = field(default_factory=list)
    suggestions: list[CandidateTag] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.escalations

    def tag_keys(self) -> list[str]:
        return [t.ref().key() for t in self.resolved_tags]
