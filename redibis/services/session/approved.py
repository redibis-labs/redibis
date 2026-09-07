"""Approved basket fragments and merge helpers."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from redibis.models import PIIDetection
from redibis.services.session.config import GlobalConfig
from redibis.services.session.state import ScanSession, split_table
from redibis.store.contract_store import ContractStore

log = logging.getLogger(__name__)

def pii_row_to_fragment(row: dict, *, masking_roles=None) -> dict:
    """Turn one PII detection row (report / discovery shape) into a single-column
    ODCS schema-property dict (name + classification + tags + privacy).

    Reuses PIIContractWriter so the fragment matches a normal scan column partial.
    """
    from redibis.pii.contract_writer import PIIContractWriter
    fields = PIIDetection.__dataclass_fields__
    kwargs = {k: row[k] for k in fields if k in row}
    kwargs.setdefault("column", row.get("column", ""))
    kwargs.setdefault("detected", bool(row.get("detected", True)))
    det = PIIDetection(**kwargs)
    writer = PIIContractWriter(database_name="_", table_name="_",
                               masking_roles=masking_roles)
    writer.add_detection(det)
    built = writer.build()
    schema = built.get("schema") or [{}]
    frag = props[0] if (props := schema[0].get("properties") or []) else {"name": det.column, "logicalType": "string"}
    telemetry = writer.build_column_telemetry().get(det.column)
    if telemetry:
        frag = dict(frag)
        frag["_column_evidence"] = telemetry
    return frag


def glossary_row_to_fragment(row: dict) -> dict:
    """Turn a glossary/definition approval row into a schema-property dict."""
    frag = {k: v for k, v in row.items() if k != "column"}
    frag.setdefault("name", row.get("column", ""))
    frag.setdefault("logicalType", "string")
    return frag


def quality_row_to_fragment(row: dict) -> dict:
    """Turn one quality rule row into a single ODCS DataQuality rule dict.

    Accepts either a GE-style row ({"expectation_type", "kwargs"[, "meta"]}) or a
    row that is already an ODCS rule ({"rule"/"type"/"engine": ...}); the latter
    passes through unchanged (minus any UI-only "column" key).
    """
    from redibis.contracts.mapper import ge_expectation_to_odcs
    if "expectation_type" in row:
        class _Exp:
            expectation_type = row.get("expectation_type")
            kwargs = row.get("kwargs", {}) or {}
            meta = row.get("meta", {}) or {}
        return ge_expectation_to_odcs(_Exp())
    if any(k in row for k in ("rule", "type", "engine")):
        return {k: v for k, v in row.items() if k != "column"}
    class _Exp2:
        expectation_type = row.get("type")
        kwargs = row.get("kwargs", {}) or {}
        meta = {}
    return ge_expectation_to_odcs(_Exp2())


def ensure_schema_base(
    session: "ScanSession",
    store: ContractStore,
    *,
    validate: bool = True,
) -> Optional[dict]:
    """Upsert a full-column schema backbone when the active contract is incomplete."""
    from redibis.contracts.schema_base import build_schema_base, missing_schema_columns
    from redibis.contracts.type_inference import dtype_map_from_dataframe
    from redibis.services.session.state import _load_dataframe

    try:
        df = _load_dataframe(session)
    except Exception:
        log.debug("ensure_schema_base: no dataframe for session %s", session.session_id)
        return None

    col_dtypes = dtype_map_from_dataframe(df)
    if not col_dtypes:
        return None

    active = store.get_active(session.table_name)
    missing = missing_schema_columns(active, list(col_dtypes.keys()))
    if active is not None and not missing:
        return None

    profiles = None
    if session.profiler is not None:
        profiles = getattr(session.profiler, "column_profiles", None)

    partial = build_schema_base(
        session.table_name,
        col_dtypes,
        df=df,
        profiles=profiles,
    )
    if session.profiler and getattr(session.profiler, "source_metadata", None):
        from redibis.profiling.metadata_tiers import apply_catalog_props_to_partial

        partial = apply_catalog_props_to_partial(
            partial, session.profiler.source_metadata,
        )

    res = store.upsert(
        partial=partial,
        table=session.table_name,
        workflow="schema",
        run_id=f"schema_{session.session_id[:12]}",
        validate=validate,
    )
    session.schema_contract_version = res.version_after
    session.add_log(
        f"Schema base: {len(col_dtypes)} column(s) → v{res.version_after}"
    )
    return res.to_dict()


def build_approved_partials(session: "ScanSession") -> Dict[str, Optional[dict]]:
    """Assemble the approved basket into at most two ODCS partials.

    Returns ``{"pii": <partial|None>, "quality": <partial|None>}``.

    The FULL basket is emitted every time (already-merged items are NOT skipped)
    so the ContractMerger's wholesale column/table ``quality[]`` replace stays
    lossless across repeated merges — i.e. re-merging is idempotent and never
    drops a previously-merged rule on the same column. Read-only; no I/O.
    """
    db_name, tbl_name = split_table(session.table_name)
    model_name = f"{db_name}_{tbl_name}"
    physical = f"{db_name}.{tbl_name}"

    pii_props: List[dict] = []
    glossary_props: List[dict] = []
    q_cols: Dict[str, List[dict]] = {}
    q_table: List[dict] = []
    column_telemetry: Dict[str, dict] = {}
    for p in session.approved.items:
        if p.kind == "pii":
            prop = dict(p.payload)
            prop.pop("_column_evidence", None)
            prop.setdefault("name", p.column)
            pii_props.append(prop)
            col = prop.get("name") or p.column
            evidence = p.payload.get("_column_evidence")
            if not evidence and (prop.get("entity_type") or p.payload.get("confidence") is not None):
                engines = []
                if p.payload.get("presidio_score"):
                    engines.append("regex")
                if p.payload.get("gliner_score"):
                    engines.append("ner")
                if p.payload.get("llm_score"):
                    engines.append("llm")
                evidence = {
                    "entity_type": prop.get("entity_type") or p.payload.get("entity_type"),
                    "confidence": p.payload.get("confidence"),
                    "discovery_engines": engines or p.payload.get("discovery_engines") or [],
                    "run_id": p.payload.get("run_id", ""),
                }
            if isinstance(evidence, dict) and col:
                column_telemetry[col] = dict(evidence)
        elif p.kind == "glossary":
            prop = dict(p.payload)
            prop.pop("_column_evidence", None)
            prop.setdefault("name", p.column)
            glossary_props.append(prop)
        elif p.kind == "quality":
            rule = dict(p.payload)
            if p.column and p.column != "__table__":
                q_cols.setdefault(p.column, []).append(rule)
            else:
                q_table.append(rule)

    def _wrap(properties: List[dict], table_quality: List[dict], *, telemetry: Optional[dict] = None) -> Optional[dict]:
        if not properties and not table_quality:
            return None
        schema_obj: Dict[str, Any] = {
            "name": model_name, "physicalName": physical, "properties": properties,
        }
        if table_quality:
            schema_obj["quality"] = table_quality
        out: Dict[str, Any] = {
            "apiVersion": "v3.0.1", "kind": "DataContract",
            "name": f"{model_name}_contract", "version": "1.0.0", "status": "active",
            "database_name": db_name, "table_name": tbl_name,
            "schema": [schema_obj],
        }
        if telemetry:
            out["_column_telemetry"] = telemetry
        return out

    pii_partial = _wrap(pii_props + glossary_props, [], telemetry=column_telemetry or None)
    q_props = [{"name": c, "logicalType": "string", "quality": rules}
               for c, rules in sorted(q_cols.items())]
    quality_partial = _wrap(q_props, q_table)
    return {"pii": pii_partial, "quality": quality_partial}


def merge_approved(session: "ScanSession", store: ContractStore,
                   validate: bool = True) -> dict:
    """Build partials from the approved basket and upsert each workflow into the
    active contract via ``ContractStore.upsert()`` (still the ONLY contract-bucket
    writer — invariant #1).

    Flips every contributing item to ``status='merged'`` and stamps
    ``merged_version``. A merge with nothing to contribute is a harmless no-op
    (returns ``noop=True`` rather than a confusing ``merged_version=None`` flag).
    """
    items = session.approved.items
    if not items or all(p.status == "merged" for p in items):
        return {"merged_version": None, "noop": True, "results": {},
                "summary": session.approved.summary()}
    partials = build_approved_partials(session)
    if all(v is None for v in partials.values()):
        return {"merged_version": None, "noop": True, "results": {},
                "summary": session.approved.summary()}

    ensure_schema_base(session, store, validate=validate)

    results: Dict[str, dict] = {}
    version_after: Optional[str] = None
    for workflow in ("pii", "quality"):
        partial = partials.get(workflow)
        if partial is None:
            continue
        res = store.upsert(partial=partial, table=session.table_name,
                           workflow=workflow, run_id=f"approved_{workflow}",
                           validate=validate)
        results[workflow] = res.to_dict()
        version_after = res.version_after
        if workflow == "pii":
            session.pii_contract_version = res.version_after
        elif workflow == "quality":
            session.quality_contract_version = res.version_after

    now = datetime.now(timezone.utc).isoformat()
    for p in session.approved.items:
        if partials.get(p.kind) is not None:
            p.status = "merged"
            p.merged_version = version_after
            p.updated_at = now

    session.add_log(f"Approved basket merged → v{version_after}")
    from redibis.memory.writer import record_approved_merge
    record_approved_merge(
        session,
        store,
        memory_config=getattr(store, "memory_config", None),
        merged_version=version_after,
    )
    from redibis.profiling.golden_writer import promote_approved_columns
    promote_approved_columns(session, store, approved_by=getattr(session, "reviewed_by", "system"))
    return {"merged_version": version_after, "results": results,
            "summary": session.approved.summary()}
