"""
redibis.memory.decision — human review decisions linked to column fingerprints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReviewDecision:
    fingerprint_key: str
    table: str
    column: str
    pii_verdict: Optional[str] = None
    masking_strategy: Optional[dict] = None
    quality_rules: list[dict] = field(default_factory=list)
    business_definition: Optional[str] = None
    classification: Optional[str] = None
    rationale: str = ""
    reviewer: str = ""
    decided_at: str = field(default_factory=_utc_now_iso)
    contract_version: str = ""
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewDecision":
        return cls(
            fingerprint_key=d.get("fingerprint_key", ""),
            table=d.get("table", ""),
            column=d.get("column", ""),
            pii_verdict=d.get("pii_verdict"),
            masking_strategy=d.get("masking_strategy"),
            quality_rules=list(d.get("quality_rules") or []),
            business_definition=d.get("business_definition"),
            classification=d.get("classification"),
            rationale=d.get("rationale", ""),
            reviewer=d.get("reviewer", ""),
            decided_at=d.get("decided_at") or _utc_now_iso(),
            contract_version=d.get("contract_version", ""),
            provenance=dict(d.get("provenance") or {}),
        )
