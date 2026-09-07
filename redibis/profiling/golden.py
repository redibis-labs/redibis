"""
redibis.profiling.golden — approved golden column records for vector similarity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from redibis.profiling.fingerprint import FEATURE_SPEC_VERSION


@dataclass
class GoldenColumn:
    """One approved column promoted to the reference set: vector + all governance."""

    table_name: str
    column_name: str
    embedding: list[float]
    fingerprint: dict
    classification: Optional[str] = None
    entity_type: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    glossary: Optional[str] = None
    synonyms: list[str] = field(default_factory=list)
    masking_policy: dict = field(default_factory=dict)
    privacy: dict = field(default_factory=dict)
    feature_spec_version: str = FEATURE_SPEC_VERSION
    contract_uuid: Optional[str] = None
    approved_by: str = "system"
    approved_at: str = ""

    def key(self) -> str:
        return f"{self.table_name}::{self.column_name}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> GoldenColumn:
        return cls(
            table_name=d["table_name"],
            column_name=d["column_name"],
            embedding=list(d.get("embedding") or []),
            fingerprint=dict(d.get("fingerprint") or {}),
            classification=d.get("classification"),
            entity_type=d.get("entity_type"),
            tags=list(d.get("tags") or []),
            glossary=d.get("glossary"),
            synonyms=list(d.get("synonyms") or []),
            masking_policy=dict(d.get("masking_policy") or {}),
            privacy=dict(d.get("privacy") or {}),
            feature_spec_version=d.get("feature_spec_version", FEATURE_SPEC_VERSION),
            contract_uuid=d.get("contract_uuid"),
            approved_by=d.get("approved_by", "system"),
            approved_at=d.get("approved_at", ""),
        )


@dataclass
class ScoredMatch:
    golden: GoldenColumn
    score: float
    distance: float

    def to_dict(self) -> dict:
        return {
            "golden": self.golden.to_dict(),
            "score": self.score,
            "distance": self.distance,
        }
