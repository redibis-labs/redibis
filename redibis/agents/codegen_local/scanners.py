"""SAST + deterministic policy checks for local codegen."""

from __future__ import annotations

import re
import warnings
from typing import Any, Optional

from redibis.agents.codegen_local.models import LocalJudgeVerdict, LocalVulnReport
from redibis.agents.codegen_local.validators import ArtifactValidation

_FORBIDDEN = (
    re.compile(r"\beval\s*\(", re.IGNORECASE),
    re.compile(r"\bexec\s*\(", re.IGNORECASE),
    re.compile(r"\b__import__\s*\(", re.IGNORECASE),
    re.compile(r"\bsubprocess\b", re.IGNORECASE),
    re.compile(r"\bos\.system\s*\(", re.IGNORECASE),
)


def sast_scan(source: str) -> LocalVulnReport:
    """Run SAST regex checks against dangerous Python functions."""
    findings: list[dict[str, Any]] = []
    for pattern in _FORBIDDEN:
        for match in pattern.finditer(source or ""):
            findings.append({
                "rule": pattern.pattern,
                "offset": match.start(),
                "severity": "high",
            })
    return LocalVulnReport(passed=not findings, findings=findings)


def policy_judge(
    *,
    source: str,
    target_system: str,
    scan: LocalVulnReport,
    model_id: str = "",
    validation: Optional[ArtifactValidation] = None,
) -> LocalJudgeVerdict:
    """Deterministic policy check (non-LLM).

    Approval requires a clean target-schema validation **and** a clean SAST scan
    (when SAST was run for the target).
    """
    mid = model_id or "redibis-policy-judge"
    if validation is not None and not validation.passed:
        detail = "; ".join(
            str(f.get("detail") or f.get("rule") or f) for f in (validation.findings or [])[:3]
        )
        return LocalJudgeVerdict(
            approved=False,
            rationale=f"artifact validation failed ({validation.validator}): {detail}",
            model_id=mid,
        )
    if not scan.passed:
        return LocalJudgeVerdict(
            approved=False,
            rationale="SAST findings must be resolved before approval",
            model_id=mid,
        )
    text = (source or "").lower()
    if target_system == "ranger":
        if "policy" not in text and "mask" not in text:
            return LocalJudgeVerdict(
                approved=False,
                rationale="Ranger output should reference policy or masking intent",
                model_id=mid,
            )
    return LocalJudgeVerdict(
        approved=True,
        rationale="Deterministic policy check approved safe, bounded, schema-valid output",
        model_id=mid,
    )


def llm_judge(
    *,
    source: str,
    target_system: str,
    scan: LocalVulnReport,
    model_id: str = "",
    validation: Optional[ArtifactValidation] = None,
) -> LocalJudgeVerdict:
    """Deprecated alias for policy_judge (deterministic, not an LLM)."""
    warnings.warn(
        "llm_judge is deprecated; use policy_judge (deterministic, non-LLM)",
        DeprecationWarning,
        stacklevel=2,
    )
    return policy_judge(
        source=source,
        target_system=target_system,
        scan=scan,
        model_id=model_id,
        validation=validation,
    )
