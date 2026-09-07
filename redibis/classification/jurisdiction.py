"""Steward-supplied jurisdiction signals for regulatory co-tags."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class JurisdictionContext:
    """Per-table jurisdiction supplied by the data steward."""

    table: str
    jurisdiction: str = ""
    data_subject_regions: list[str] = field(default_factory=list)
    notes: str = ""

    def normalized_jurisdiction(self) -> str:
        return (self.jurisdiction or "").strip().upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "jurisdiction": self.jurisdiction,
            "data_subject_regions": list(self.data_subject_regions),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JurisdictionContext":
        return cls(
            table=str(data.get("table") or ""),
            jurisdiction=str(data.get("jurisdiction") or ""),
            data_subject_regions=list(data.get("data_subject_regions") or []),
            notes=str(data.get("notes") or ""),
        )


def jurisdiction_for_table(
    contexts: dict[str, JurisdictionContext],
    table: str,
    *,
    default: str = "",
) -> str:
    ctx = contexts.get(table)
    if ctx and ctx.jurisdiction:
        return ctx.normalized_jurisdiction()
    return default.strip().upper()
