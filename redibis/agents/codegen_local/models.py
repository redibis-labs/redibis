"""Local codegen dataclasses for OSS proposed code generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LocalCodeProposal:
    """Proposed code artifact — generated locally, never executed automatically."""

    language: str
    source: str
    purpose: str
    table: str = ""
    generation_source: str = "template"  # 'template' | 'local_llm'
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "purpose": self.purpose,
            "table": self.table,
            "generation_source": self.generation_source,
            "metadata": dict(self.metadata),
        }


@dataclass
class LocalVulnReport:
    """SAST scanner report."""

    passed: bool
    findings: list[dict[str, Any]] = field(default_factory=list)
    scanner: str = "sast-regex"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": list(self.findings),
            "scanner": self.scanner,
        }


@dataclass
class LocalJudgeVerdict:
    """Deterministic policy check verdict."""

    approved: bool
    rationale: str = ""
    model_id: str = "redibis-policy-judge"

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "rationale": self.rationale,
            "model_id": self.model_id,
        }
