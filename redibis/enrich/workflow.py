"""YAML-defined enrichment workflows with prebuilt stage kinds."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import yaml

PREBUILT_STAGE_KINDS = frozenset({
    "column_definitions",
    "classification_pii",
    "table_definition",
    "contract_review",
    "validate",
})

# Stages that call the LLM and therefore have prompt folders.
LLM_STAGE_KINDS = frozenset({
    "column_definitions",
    "classification_pii",
    "table_definition",
    "contract_review",
})

DEFAULT_STEPS: list[dict[str, Any]] = [
    {"id": "columns", "kind": "column_definitions", "enabled": True},
    {"id": "privacy", "kind": "classification_pii", "enabled": True},
    {"id": "table", "kind": "table_definition", "enabled": True},
    {"id": "review", "kind": "contract_review", "enabled": True},
]


@dataclass
class EnrichmentStep:
    id: str
    kind: str
    enabled: bool = True
    max_attempts: Optional[int] = None
    instructions: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnrichmentWorkflow:
    version: int = 1
    max_attempts: int = 3
    rai_enabled: Optional[bool] = None
    steps: list[EnrichmentStep] = field(default_factory=list)

    def enabled_steps(self) -> list[EnrichmentStep]:
        return [s for s in self.steps if s.enabled]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "max_attempts": self.max_attempts,
            "rai_enabled": self.rai_enabled,
            "steps": [s.to_dict() for s in self.steps],
        }


def default_workflow(*, max_attempts: int = 3) -> EnrichmentWorkflow:
    return parse_workflow({"version": 1, "max_attempts": max_attempts, "steps": DEFAULT_STEPS})


def load_workflow_file(path: Union[str, Path], *, default_max_attempts: int = 3) -> EnrichmentWorkflow:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"enrichment steps file not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"enrichment steps file must be a mapping: {p}")
    data.setdefault("max_attempts", default_max_attempts)
    return parse_workflow(data)


def parse_workflow(data: dict[str, Any]) -> EnrichmentWorkflow:
    version = int(data.get("version") or 1)
    if version != 1:
        raise ValueError(f"unsupported enrichment workflow version: {version}")

    max_attempts = int(data.get("max_attempts") or 3)
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    rai_enabled = data.get("rai_enabled")
    if rai_enabled is not None:
        rai_enabled = bool(rai_enabled)

    raw_steps = data.get("steps")
    if raw_steps is None:
        raw_steps = DEFAULT_STEPS
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("workflow.steps must be a non-empty list")

    steps: list[EnrichmentStep] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise ValueError(f"workflow.steps[{idx}] must be a mapping")
        step_id = str(item.get("id") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        if not step_id:
            raise ValueError(f"workflow.steps[{idx}] missing id")
        if step_id in seen_ids:
            raise ValueError(f"duplicate workflow step id: {step_id!r}")
        seen_ids.add(step_id)
        if kind not in PREBUILT_STAGE_KINDS:
            raise ValueError(
                f"unknown workflow step kind {kind!r}; "
                f"allowed: {sorted(PREBUILT_STAGE_KINDS)}"
            )
        step_attempts = item.get("max_attempts")
        if step_attempts is not None:
            step_attempts = int(step_attempts)
            if step_attempts < 1:
                raise ValueError(f"step {step_id!r} max_attempts must be >= 1")
        steps.append(EnrichmentStep(
            id=step_id,
            kind=kind,
            enabled=bool(item.get("enabled", True)),
            max_attempts=step_attempts,
            instructions=str(item.get("instructions") or ""),
        ))

    enabled = [s for s in steps if s.enabled]
    if not enabled:
        raise ValueError("workflow has no enabled steps")

    return EnrichmentWorkflow(
        version=version,
        max_attempts=max_attempts,
        rai_enabled=rai_enabled,
        steps=steps,
    )


def stage_prompt_guidance(kind: str) -> str:
    """Extra system guidance appended for a stage."""
    if kind == "column_definitions":
        return (
            "STAGE=column_definitions. Return ONLY column businessName/business/tags. "
            "Do not emit pii blocks or table.description."
        )
    if kind == "classification_pii":
        return (
            "STAGE=classification_pii. Return ONLY columns[].pii (and optional governance tags). "
            "Do not rewrite business definitions or table.description."
        )
    if kind == "table_definition":
        return (
            "STAGE=table_definition. Return ONLY top-level table.description "
            "(required) and optional table_tags / table.purpose. Do not emit columns."
        )
    if kind == "contract_review":
        return (
            "STAGE=contract_review. Review the WHOLE enriched contract for consistency. "
            "Return ONLY corrections (union of column business/pii/tags plus table/"
            "table_tags) and an optional review.findings list. "
            "Empty columns/table is valid when there are no findings. "
            "Every change needs a one-line reason (pii.reason or a finding message). "
            "Do not re-emit schema, quality rules, or the full contract."
        )
    if kind == "validate":
        return "STAGE=validate. No LLM call — ODCS validation only."
    return ""
