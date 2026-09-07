"""Reconcile requirement intent vs implementation vs base contract."""

from __future__ import annotations

from typing import Any, Optional

from redibis.synthesis.evidence import (
    EvidenceBundle,
    EvidenceKind,
    EvidenceRecord,
    TraceStatus,
)


def reconcile(
    bundle: EvidenceBundle,
    *,
    base_contract: Optional[dict[str, Any]] = None,
) -> EvidenceBundle:
    """
    Cross-check requirements fields vs code transforms and optional base schema.

    Mutates ``bundle`` in place (conflicts / unresolved / traceability updates).
    """
    base_cols: set[str] = set()
    if base_contract:
        for schema_obj in base_contract.get("schema") or []:
            for prop in schema_obj.get("properties") or []:
                name = prop.get("name")
                if name:
                    base_cols.add(str(name))

    req_fields = {
        (r.payload or {}).get("name"): r
        for r in bundle.by_kind(EvidenceKind.SCHEMA_FIELD)
        if (r.payload or {}).get("name")
    }
    code_cols = {
        (r.payload or {}).get("target_column"): r
        for r in bundle.by_kind(EvidenceKind.COLUMN_TRANSFORM)
        if (r.payload or {}).get("target_column")
    }

    # Requirements fields missing from base contract schema.
    for name, rec in req_fields.items():
        if base_cols and name not in base_cols:
            conflict = EvidenceRecord(
                id=f"conflict:missing-in-contract:{name}",
                kind=EvidenceKind.CONFLICT,
                summary=f"Requirements field {name!r} absent from base contract schema",
                confidence=0.9,
                requirement_ids=list(rec.requirement_ids),
                payload={"field": name, "issue": "missing_in_contract"},
                source="reconcile",
            )
            bundle.conflicts.append(conflict)

        # Type mismatch when base has the column.
        if base_contract and name in base_cols:
            base_prop = _find_prop(base_contract, name)
            req_type = str((rec.payload or {}).get("logicalType") or "").lower()
            base_type = str((base_prop or {}).get("logicalType") or "").lower()
            if req_type and base_type and req_type != base_type:
                bundle.conflicts.append(EvidenceRecord(
                    id=f"conflict:type:{name}",
                    kind=EvidenceKind.CONFLICT,
                    summary=(
                        f"logicalType mismatch for {name}: "
                        f"requirements={req_type} contract={base_type}"
                    ),
                    confidence=0.85,
                    requirement_ids=list(rec.requirement_ids),
                    payload={
                        "field": name,
                        "issue": "type_mismatch",
                        "requirements": req_type,
                        "contract": base_type,
                    },
                    source="reconcile",
                ))

    # Code columns not mentioned in requirements (informational unresolved).
    for name, rec in code_cols.items():
        if name and name not in req_fields and (not base_cols or name not in base_cols):
            bundle.unresolved.append(EvidenceRecord(
                id=f"unresolved:code-only:{name}",
                kind=EvidenceKind.UNRESOLVED,
                summary=f"Code defines column {name!r} with no matching requirements field",
                confidence=0.5,
                payload={"field": name},
                source="reconcile",
            ))

    # Update traceability for field requirement IDs when evidence exists.
    by_req: dict[str, list[EvidenceRecord]] = {}
    for rec in bundle.records:
        for rid in rec.requirement_ids:
            by_req.setdefault(rid, []).append(rec)

    for row in bundle.traceability:
        if row.status == TraceStatus.DOC_ONLY:
            continue
        linked = by_req.get(row.requirement_id) or []
        if any(r.kind == EvidenceKind.CONFLICT for r in bundle.conflicts if row.requirement_id in r.requirement_ids):
            row.status = TraceStatus.CONFLICT
        elif linked:
            row.status = TraceStatus.MAPPED
            row.evidence_ids = sorted(set(row.evidence_ids) | {r.id for r in linked})
            # Guess a contract path for field reqs.
            for r in linked:
                fname = (r.payload or {}).get("name") or (r.payload or {}).get("target_column")
                if fname:
                    path = f"schema[0].properties[{fname}]"
                    if path not in row.contract_paths:
                        row.contract_paths.append(path)
        else:
            row.status = TraceStatus.UNRESOLVED

    bundle.meta["reconcile"] = {
        "req_fields": len(req_fields),
        "code_columns": len(code_cols),
        "base_columns": len(base_cols),
        "conflicts": len(bundle.conflicts),
        "unresolved": len(bundle.unresolved),
    }
    return bundle


def _find_prop(contract: dict[str, Any], name: str) -> Optional[dict[str, Any]]:
    for schema_obj in contract.get("schema") or []:
        for prop in schema_obj.get("properties") or []:
            if prop.get("name") == name:
                return prop
    return None
