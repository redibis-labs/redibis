"""Pipeline board models — graph spec and compiled prompt-plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4


def _new_id() -> str:
    return uuid4().hex[:12]


@dataclass
class PipelineNode:
    """One node on the agentic board — maps to a deterministic tool."""

    id: str = field(default_factory=_new_id)
    kind: str = ""
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    position: dict[str, float] = field(default_factory=lambda: {"x": 0, "y": 0})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "params": dict(self.params),
            "position": dict(self.position),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineNode":
        return cls(
            id=str(data.get("id") or _new_id()),
            kind=str(data.get("kind") or ""),
            label=str(data.get("label") or ""),
            params=dict(data.get("params") or {}),
            position=dict(data.get("position") or {"x": 0, "y": 0}),
        )


@dataclass
class PipelineEdge:
    """Directed edge between pipeline nodes."""

    id: str = field(default_factory=_new_id)
    source: str = ""
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "target": self.target}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineEdge":
        return cls(
            id=str(data.get("id") or _new_id()),
            source=str(data.get("source") or ""),
            target=str(data.get("target") or ""),
        )


@dataclass
class PipelineSpec:
    """Full board graph — serializable to JSON for export and v2 execution."""

    name: str = "untitled"
    goal: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    nodes: list[PipelineNode] = field(default_factory=list)
    edges: list[PipelineEdge] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "goal": self.goal,
            "source": dict(self.source),
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineSpec":
        return cls(
            name=str(data.get("name") or "untitled"),
            goal=str(data.get("goal") or ""),
            source=dict(data.get("source") or {}),
            nodes=[PipelineNode.from_dict(n) for n in (data.get("nodes") or [])],
            edges=[PipelineEdge.from_dict(e) for e in (data.get("edges") or [])],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class PromptPlanSection:
    """One editable section of the compiled prompt-plan."""

    key: str
    title: str
    content: str
    locked: bool = False
    source_nodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "content": self.content,
            "locked": self.locked,
            "source_nodes": list(self.source_nodes),
        }


@dataclass
class PromptPlan:
    """Export-only v1 artifact — editable plan referencing deterministic tools."""

    goal: str = ""
    source: str = ""
    steps: str = ""
    guardrails: str = ""
    output_format: str = ""
    approval_gates: str = ""
    sections: list[PromptPlanSection] = field(default_factory=list)
    pipeline: Optional[PipelineSpec] = None

    def to_text(self) -> str:
        parts = [
            "# Goal",
            self.goal,
            "",
            "# Source",
            self.source,
            "",
            "# Steps",
            self.steps,
            "",
            "# Guardrails (locked)",
            self.guardrails,
            "",
            "# Output format",
            self.output_format,
            "",
            "# Approval gates",
            self.approval_gates,
        ]
        return "\n".join(parts).strip() + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source": self.source,
            "steps": self.steps,
            "guardrails": self.guardrails,
            "output_format": self.output_format,
            "approval_gates": self.approval_gates,
            "sections": [s.to_dict() for s in self.sections],
            "pipeline": self.pipeline.to_dict() if self.pipeline else None,
            "text": self.to_text(),
        }
