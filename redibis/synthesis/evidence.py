"""Typed evidence records for Contract Synthesis."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class EvidenceKind(str, Enum):
    REQUIREMENT = "requirement"
    SCHEMA_FIELD = "schema_field"
    QUALITY_RULE = "quality_rule"
    SLA = "sla"
    SECURITY = "security"
    TEAM = "team"
    SUPPORT = "support"
    SERVER = "server"
    CUSTOM = "custom"
    TABLE_READ = "table_read"
    TABLE_WRITE = "table_write"
    COLUMN_TRANSFORM = "column_transform"
    JOIN = "join"
    LOOKUP = "lookup"
    FOREIGN_KEY = "foreign_key"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"


class TraceStatus(str, Enum):
    MAPPED = "mapped"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"
    DOC_ONLY = "doc_only"


@dataclass
class EvidenceSpan:
    path: str
    start_line: int = 0
    end_line: int = 0
    extractor: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceRecord:
    """One atomic finding from requirements or source analysis."""

    id: str
    kind: EvidenceKind
    summary: str
    confidence: float = 1.0
    requirement_ids: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    spans: list[EvidenceSpan] = field(default_factory=list)
    source: str = ""  # requirements | sql | spark | datastage | contract | assisted

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value if isinstance(self.kind, EvidenceKind) else str(self.kind),
            "summary": self.summary,
            "confidence": self.confidence,
            "requirement_ids": list(self.requirement_ids),
            "payload": dict(self.payload),
            "spans": [s.to_dict() for s in self.spans],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceRecord":
        spans = [
            EvidenceSpan(**s) if isinstance(s, dict) else s
            for s in (data.get("spans") or [])
        ]
        kind_raw = data.get("kind") or EvidenceKind.CUSTOM.value
        try:
            kind = EvidenceKind(kind_raw)
        except ValueError:
            kind = EvidenceKind.CUSTOM
        return cls(
            id=str(data.get("id") or ""),
            kind=kind,
            summary=str(data.get("summary") or ""),
            confidence=float(data.get("confidence") or 1.0),
            requirement_ids=[str(x) for x in (data.get("requirement_ids") or [])],
            payload=dict(data.get("payload") or {}),
            spans=spans,
            source=str(data.get("source") or ""),
        )


@dataclass
class TraceabilityRow:
    requirement_id: str
    status: TraceStatus
    contract_paths: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "status": self.status.value if isinstance(self.status, TraceStatus) else str(self.status),
            "contract_paths": list(self.contract_paths),
            "evidence_ids": list(self.evidence_ids),
            "notes": self.notes,
        }


@dataclass
class EvidenceBundle:
    """All evidence for one synthesis run."""

    records: list[EvidenceRecord] = field(default_factory=list)
    conflicts: list[EvidenceRecord] = field(default_factory=list)
    unresolved: list[EvidenceRecord] = field(default_factory=list)
    traceability: list[TraceabilityRow] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def add(self, record: EvidenceRecord) -> None:
        self.records.append(record)

    def by_kind(self, kind: EvidenceKind) -> list[EvidenceRecord]:
        return [r for r in self.records if r.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "conflicts": [r.to_dict() for r in self.conflicts],
            "unresolved": [r.to_dict() for r in self.unresolved],
            "traceability": [t.to_dict() for t in self.traceability],
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceBundle":
        bundle = cls(meta=dict(data.get("meta") or {}))
        bundle.records = [EvidenceRecord.from_dict(r) for r in (data.get("records") or [])]
        bundle.conflicts = [EvidenceRecord.from_dict(r) for r in (data.get("conflicts") or [])]
        bundle.unresolved = [EvidenceRecord.from_dict(r) for r in (data.get("unresolved") or [])]
        for row in data.get("traceability") or []:
            status_raw = row.get("status") or TraceStatus.UNRESOLVED.value
            try:
                status = TraceStatus(status_raw)
            except ValueError:
                status = TraceStatus.UNRESOLVED
            bundle.traceability.append(TraceabilityRow(
                requirement_id=str(row.get("requirement_id") or ""),
                status=status,
                contract_paths=list(row.get("contract_paths") or []),
                evidence_ids=list(row.get("evidence_ids") or []),
                notes=str(row.get("notes") or ""),
            ))
        return bundle
