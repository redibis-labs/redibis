"""Role-routed approval gates — the 'Who Verifies' matrix."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from redibis.classification.models import ClassificationResult


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


@dataclass
class ApprovalGate:
    """A policy-gated approval checkpoint for a classification result."""

    table: str
    column: str = ""
    role: str = "Steward"
    status: ApprovalStatus = ApprovalStatus.PENDING
    classification: ClassificationResult | None = None
    reviewer: str = ""
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_human(self) -> bool:
        if self.classification and self.classification.escalations:
            return True
        return self.status == ApprovalStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "role": self.role,
            "status": self.status.value,
            "reviewer": self.reviewer,
            "notes": self.notes,
            "requires_human": self.requires_human,
            "escalations": list(self.classification.escalations) if self.classification else [],
            "violations": list(self.classification.violations) if self.classification else [],
            "tags": self.classification.tag_keys() if self.classification else [],
        }


def gate_for_result(result: ClassificationResult) -> ApprovalGate:
    """Build an approval gate from a classification result."""
    status = ApprovalStatus.PENDING
    if result.escalations:
        status = ApprovalStatus.ESCALATED
    elif result.violations:
        status = ApprovalStatus.PENDING
    return ApprovalGate(
        table=result.table,
        column=result.column,
        role=result.approval_role,
        status=status,
        classification=result,
    )
