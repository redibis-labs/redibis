"""Normalize engine, contract, and LLM proposal outputs into column actuals."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from redibis.contracts.privacy import col_entity_type, col_privacy_classification, column_is_pii
from redibis.evaluation.schema import empty_actual


def from_engine_detections(detections: Sequence[Any], *, source: str = "engine") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for det in detections:
        if isinstance(det, Mapping):
            name = str(det.get("column") or "")
            is_pii = bool(det.get("detected"))
            entity = str(det.get("entity_type") or "").upper()
        else:
            name = str(getattr(det, "column", "") or "")
            is_pii = bool(getattr(det, "detected", False))
            entity = str(getattr(det, "entity_type", "") or "").upper()
        if not name:
            continue
        row = empty_actual(name)
        row.update({
            "is_pii": is_pii,
            "entity_type": entity if is_pii else "",
            "source": source,
            "is_proposal": False,
        })
        out.append(row)
    return out


def from_contract(contract: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(contract, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for schema_obj in contract.get("schema") or []:
        for prop in schema_obj.get("properties") or []:
            if not isinstance(prop, Mapping):
                continue
            name = str(prop.get("name") or "")
            if not name:
                continue
            business = prop.get("business") if isinstance(prop.get("business"), Mapping) else {}
            row = empty_actual(name)
            is_pii = column_is_pii(dict(prop))
            row.update({
                "is_pii": is_pii,
                "entity_type": col_entity_type(dict(prop)) if is_pii else "",
                "logicalType": str(prop.get("logicalType") or ""),
                "privacy_classification": col_privacy_classification(dict(prop)),
                "businessName": str(prop.get("businessName") or ""),
                "description": str(prop.get("description") or ""),
                "business_definition": str(business.get("definition") or ""),
                "tags": [str(t) for t in (prop.get("tags") or []) if str(t).strip()],
                "source": "contract",
                "is_proposal": False,
            })
            out.append(row)
    return out


def from_llm_proposals(detections: Sequence[Any]) -> list[dict[str, Any]]:
    rows = from_engine_detections(detections, source="llm")
    participation: dict[str, bool] = {}
    for detection in detections:
        if isinstance(detection, Mapping):
            name = str(detection.get("column") or "")
            participated = detection.get("llm_score") is not None
        else:
            name = str(getattr(detection, "column", "") or "")
            participated = getattr(detection, "llm_score", None) is not None
        if name:
            participation[name] = participated
    for row in rows:
        participated = participation.get(row["name"], False)
        row["is_proposal"] = participated
        row["source"] = "llm" if participated else "engine"
    return rows
