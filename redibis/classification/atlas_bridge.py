"""Bridge classification results to Atlas atomic co-tag payloads."""

from __future__ import annotations

from redibis.classification.models import ClassificationResult, ResolvedTag


def classifications_for_atlas(result: ClassificationResult) -> list[dict]:
    """Convert resolved tags to Atlas classification payloads (one POST batch)."""
    out: list[dict] = []
    for tag in result.resolved_tags:
        if tag.domain == "RegulatoryCompliance" and tag.tag == "LawfulIntercept":
            continue
        out.append(tag.to_atlas())
    return out


def atlas_classifications_preview(
    results: list[ClassificationResult],
) -> dict:
    """Preview atomic classification POST for a table's columns."""
    columns: dict[str, list[dict]] = {}
    escalations: list[str] = []
    for result in results:
        key = result.column or result.table
        columns[key] = classifications_for_atlas(result)
        escalations.extend(result.escalations)
    return {
        "atomic": True,
        "columns": columns,
        "escalations": escalations,
        "verify_readback": True,
    }
