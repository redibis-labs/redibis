"""Selective merge of steward-accepted Deep Enrich paths into the active contract.

Only accepted/edited paths become an ODCS partial. PII/classification authority
still goes through decision overlays; ``ContractStore.upsert`` remains the sole
active-contract writer.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.synthesis.path_diff import get_value_at_path, set_value_on_partial

ACCEPT_DECISIONS = frozenset({"accept", "edit"})
REVIEW_DECISIONS = frozenset({"accept", "reject", "needs_review", "no_action", "edit"})


@dataclass
class PathVerdict:
    path: str
    decision: str
    value: Any = None
    chosen_source: str = "llm_synthesis"
    rationale_code: str = ""
    rationale_text: str = ""
    column: Optional[str] = None
    field: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "decision": self.decision,
            "value": self.value,
            "chosen_source": self.chosen_source,
            "rationale_code": self.rationale_code,
            "rationale_text": self.rationale_text,
            "column": self.column,
            "field": self.field,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PathVerdict":
        return cls(
            path=str(d.get("path") or ""),
            decision=str(d.get("decision") or ""),
            value=d.get("value"),
            chosen_source=str(d.get("chosen_source") or "llm_synthesis"),
            rationale_code=str(d.get("rationale_code") or ""),
            rationale_text=str(d.get("rationale_text") or ""),
            column=d.get("column"),
            field=d.get("field"),
        )


@dataclass
class MergePreview:
    partial: dict
    accepted_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    pii_overlay: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "partial": self.partial,
            "accepted_paths": list(self.accepted_paths),
            "skipped_paths": list(self.skipped_paths),
            "pii_overlay": list(self.pii_overlay),
            "errors": list(self.errors),
        }


def build_partial_from_verdicts(
    *,
    active: dict,
    candidate: dict,
    verdicts: list[PathVerdict | dict],
) -> MergePreview:
    """Build a mergeable ODCS partial from accepted/edited path verdicts."""
    preview = MergePreview(partial={
        "apiVersion": active.get("apiVersion") or "v3.0.1",
        "kind": "DataContract",
        "database_name": active.get("database_name") or "",
        "table_name": active.get("table_name") or "",
        "contract_uuid": active.get("contract_uuid") or "",
    })
    if not verdicts:
        preview.errors.append("no path verdicts provided")
        return preview

    for raw in verdicts:
        fv = raw if isinstance(raw, PathVerdict) else PathVerdict.from_dict(raw or {})
        if not fv.path:
            preview.errors.append("path verdict missing path")
            continue
        if fv.decision not in REVIEW_DECISIONS:
            preview.errors.append(f"invalid decision {fv.decision!r} for {fv.path}")
            continue
        if fv.decision not in ACCEPT_DECISIONS:
            preview.skipped_paths.append(fv.path)
            continue

        value = fv.value
        if value is None and fv.decision == "accept":
            # Prefer candidate; fall back to chosen_source semantics
            value = get_value_at_path(candidate, fv.path)

        # PII paths go through overlay, not raw schema merge
        if fv.path.endswith(".pii") or (fv.field == "pii"):
            col = fv.column or _column_from_path(fv.path)
            if not col:
                preview.errors.append(f"pii path missing column: {fv.path}")
                continue
            is_pii = False
            entity = None
            if isinstance(value, dict):
                is_pii = bool(value.get("is_pii") or value.get("detected"))
                entity = value.get("entity_type")
            elif isinstance(value, bool):
                is_pii = value
            preview.pii_overlay.append({
                "column": col,
                "status": "pii" if is_pii else "not_pii",
                "entity_type": entity,
                "chosen_source": fv.chosen_source,
                "path": fv.path,
            })
            preview.accepted_paths.append(fv.path)
            continue

        try:
            set_value_on_partial(preview.partial, fv.path, value, active=active)
            preview.accepted_paths.append(fv.path)
        except Exception as exc:
            preview.errors.append(f"{fv.path}: {exc}")

    return preview


def _column_from_path(path: str) -> Optional[str]:
    if not path.startswith("schema.properties."):
        return None
    rest = path[len("schema.properties."):]
    col, _, _ = rest.partition(".")
    return col or None


def apply_pii_overlays(
    store,
    table: str,
    overlays: list[dict],
    *,
    decided_by: str,
    run_id: str = "",
) -> list[dict]:
    """Persist PII decisions for accepted Deep Enrich paths via ContractStore."""
    results = []
    for item in overlays:
        col = item.get("column")
        if not col:
            continue
        status = item.get("status") or "not_pii"
        result = store.set_pii_decision(
            table,
            col,
            status,
            entity_type=item.get("entity_type"),
            decided_by=decided_by,
            run_id=run_id or "deep-enrich-merge",
        )
        results.append({
            "column": col,
            "status": status,
            "version_after": getattr(result, "version_after", None),
        })
    return results


def merge_accepted(
    store,
    table: str,
    *,
    active: dict,
    candidate: dict,
    verdicts: list[PathVerdict | dict],
    decided_by: str,
    run_id: str = "",
    validate: bool = True,
) -> dict[str, Any]:
    """Preview + upsert accepted paths. Returns merge result metadata."""
    preview = build_partial_from_verdicts(
        active=active, candidate=candidate, verdicts=verdicts,
    )
    if preview.errors and not preview.accepted_paths:
        return {"ok": False, "errors": preview.errors, "preview": preview.to_dict()}

    upsert_result = None
    # Only upsert when the partial carries more than identity fields
    carry = {
        k: v for k, v in preview.partial.items()
        if k not in ("apiVersion", "kind", "database_name", "table_name", "contract_uuid")
    }
    if carry:
        upsert_result = store.upsert(
            preview.partial,
            table=table,
            workflow="deep_enrich",
            run_id=run_id or "deep-enrich-merge",
            validate=validate,
        )

    pii_results = []
    if preview.pii_overlay:
        pii_results = apply_pii_overlays(
            store, table, preview.pii_overlay,
            decided_by=decided_by,
            run_id=run_id or "deep-enrich-merge",
        )

    return {
        "ok": True,
        "errors": preview.errors,
        "accepted_paths": preview.accepted_paths,
        "skipped_paths": preview.skipped_paths,
        "pii_overlay": pii_results,
        "version_before": getattr(upsert_result, "version_before", None),
        "version_after": getattr(upsert_result, "version_after", None),
        "run_uuid": getattr(upsert_result, "run_uuid", None),
        "preview": preview.to_dict(),
    }


__all__ = [
    "PathVerdict",
    "MergePreview",
    "ACCEPT_DECISIONS",
    "REVIEW_DECISIONS",
    "build_partial_from_verdicts",
    "merge_accepted",
    "apply_pii_overlays",
]
