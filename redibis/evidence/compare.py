"""Compare deterministic / single-call LLM / agentic results without extra model calls."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from redibis.evidence.models import (
    COMPARISON_KIND,
    CoverageStatus,
    ExecutionMode,
    ResultVariant,
    SCHEMA_VERSION,
    VARIANTS_KIND,
)


def _column_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"is_pii": False, "entity_type": "", "classification": "", "confidence": None}
    return {
        "is_pii": bool(payload.get("is_pii") or payload.get("detected")),
        "entity_type": payload.get("entity_type") or "",
        "classification": payload.get("classification") or "",
        "confidence": payload.get("confidence"),
    }


def variant(
    mode: str,
    *,
    present: bool,
    reason: str = "",
    artifact: str = "",
    summary: Optional[dict[str, Any]] = None,
    columns: Optional[dict[str, Any]] = None,
    steps: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    status = CoverageStatus.EVALUATED.value if present else CoverageStatus.NOT_RUN.value
    return ResultVariant(
        mode=mode,
        status=status,
        reason="" if present else (reason or "mode was not executed"),
        artifact=artifact,
        summary=dict(summary or {}),
        columns=dict(columns or {}),
        steps=list(steps or []),
    ).to_dict()


def pii_columns_from_detections(detections: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for det in detections:
        if isinstance(det, dict):
            name = str(det.get("column") or "")
            payload = det
        else:
            name = str(getattr(det, "column", "") or "")
            payload = {
                "detected": getattr(det, "detected", False),
                "entity_type": getattr(det, "entity_type", None),
                "confidence": getattr(det, "confidence", None),
                "classification": getattr(det, "classification", ""),
            }
        if name:
            out[name] = _column_verdict(payload)
    return out


def profile_columns_from_result(result: Any) -> dict[str, Any]:
    """Comparable per-column profile summaries (not an empty placeholder)."""
    out: dict[str, Any] = {}
    dtypes = getattr(result, "col_dtypes", None) or {}
    if isinstance(dtypes, dict):
        for name, dtype in dtypes.items():
            if name:
                out[str(name)] = {
                    "is_pii": False,
                    "entity_type": "",
                    "classification": "",
                    "confidence": None,
                    "dtype": str(dtype),
                }
    profiles = getattr(getattr(result, "profile", None), "column_profiles", None)
    if isinstance(profiles, (list, tuple)):
        for prof in profiles:
            name = str(getattr(prof, "column", "") or "")
            if not name:
                continue
            block = out.setdefault(name, {
                "is_pii": False,
                "entity_type": "",
                "classification": "",
                "confidence": None,
            })
            for attr in ("null_ratio", "distinct_count", "inferred_type", "dtype"):
                if hasattr(prof, attr):
                    block[attr] = getattr(prof, attr)
    return out


def compare_column_maps(
    variants: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """``variants`` maps mode → {column: verdict} for modes that actually ran."""
    ran = {mode: cols for mode, cols in variants.items() if cols is not None}
    names: set[str] = set()
    for cols in ran.values():
        names.update(cols.keys())
    columns: list[dict[str, Any]] = []
    diverge = 0
    for name in sorted(names):
        by_mode = {mode: cols.get(name, {}) for mode, cols in ran.items()}
        verdicts = list(by_mode.values())
        agreement = True
        if len(verdicts) >= 2:
            first = (verdicts[0].get("is_pii"), verdicts[0].get("entity_type"))
            agreement = all(
                (v.get("is_pii"), v.get("entity_type")) == first for v in verdicts[1:]
            )
        if not agreement:
            diverge += 1
        columns.append({
            "column": name,
            "agreement": agreement,
            "by_mode": by_mode,
        })
    return {
        "kind": COMPARISON_KIND,
        "schema_version": SCHEMA_VERSION,
        "modes_compared": sorted(ran.keys()),
        "column_count": len(names),
        "divergent_count": diverge,
        "columns": columns,
    }


def build_result_bundle(
    *,
    table: str,
    run_id: str,
    deterministic: Optional[dict[str, Any]] = None,
    single_llm: Optional[dict[str, Any]] = None,
    agentic: Optional[dict[str, Any]] = None,
    agentic_steps: Optional[list[dict[str, Any]]] = None,
    artifacts: Optional[dict[str, str]] = None,
    domain: str = "pii",
) -> dict[str, Any]:
    """Assemble variants + comparison. Missing modes are ``not_run`` — never invoked."""
    arts = artifacts or {}
    variants = [
        variant(
            ExecutionMode.DETERMINISTIC.value,
            present=deterministic is not None,
            artifact=arts.get("deterministic", ""),
            columns=deterministic or {},
        ),
        variant(
            ExecutionMode.SINGLE_LLM.value,
            present=single_llm is not None,
            artifact=arts.get("single_llm", ""),
            columns=single_llm or {},
        ),
        variant(
            ExecutionMode.AGENTIC.value,
            present=agentic is not None,
            artifact=arts.get("agentic", ""),
            columns=agentic or {},
            steps=list(agentic_steps or []),
        ),
    ]
    ran = {}
    if deterministic is not None:
        ran[ExecutionMode.DETERMINISTIC.value] = deterministic
    if single_llm is not None:
        ran[ExecutionMode.SINGLE_LLM.value] = single_llm
    if agentic is not None:
        ran[ExecutionMode.AGENTIC.value] = agentic
    comparison = compare_column_maps(ran)
    comparison["table"] = table
    comparison["run_id"] = run_id
    comparison["domain"] = domain
    return {
        "variants": {
            "kind": VARIANTS_KIND,
            "schema_version": SCHEMA_VERSION,
            "table": table,
            "run_id": run_id,
            "domain": domain,
            "variants": variants,
        },
        "comparison": comparison,
    }
