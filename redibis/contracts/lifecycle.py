"""
Contract lifecycle helpers — run artifacts and provenance (no new storage writers).
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from redibis.contracts.contract_diff import diff_contracts, render_diff_md
from redibis.store.run_output_writer import RunOutputWriter


def _contract_column_verdicts(contract: dict) -> dict[str, Any]:
    from redibis.contracts.privacy import column_is_pii, col_pii_engine

    out: dict[str, Any] = {}
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict) or not prop.get("name"):
                continue
            ce = col_pii_engine(prop)
            out[str(prop["name"])] = {
                "is_pii": column_is_pii(prop),
                "entity_type": ce.get("entity_type") or prop.get("entity_type") or "",
                "classification": prop.get("classification") or "",
                "confidence": ce.get("confidence"),
            }
    return out


def write_deterministic_snapshot(
    run_writer: RunOutputWriter,
    contract: dict,
    *,
    run_id: str,
    version: Optional[str] = None,
) -> dict[str, str]:
    """Write per-run deterministic contract snapshot + active pointer."""
    keys: dict[str, str] = {}
    keys["contract.deterministic.yaml"] = run_writer.write(
        "contract.deterministic.yaml", contract,
    )
    ref = {
        "active_version": version or contract.get("version"),
        "source": "deterministic",
        "run_id": run_id,
    }
    keys["contract.active.ref.json"] = run_writer.write("contract.active.ref.json", ref)
    return keys


def write_llm_lifecycle_artifacts(
    run_writer: RunOutputWriter,
    c_det: dict,
    c_llm: dict,
    *,
    run_id: str,
    active_version: Optional[str] = None,
    det_version: Optional[str] = None,
    agentic: bool = False,
    agentic_steps: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Write LLM contract, diff artifacts, and active pointer to the run folder."""
    c_llm_clean = copy.deepcopy(c_llm)
    c_llm_clean.pop("enrichment_meta", None)
    diff = diff_contracts(c_det, c_llm_clean)
    keys: dict[str, str] = {}
    keys["contract.llm.yaml"] = run_writer.write("contract.llm.yaml", c_llm_clean)
    keys["contract_diff.json"] = run_writer.write("contract_diff.json", diff)
    keys["contract_diff.md"] = run_writer.write("contract_diff.md", render_diff_md(diff))
    try:
        from redibis.evidence.compare import build_result_bundle

        det_cols = _contract_column_verdicts(c_det)
        llm_cols = _contract_column_verdicts(c_llm_clean)
        bundle = build_result_bundle(
            table=str(c_det.get("name") or ""),
            run_id=run_id,
            deterministic=det_cols,
            single_llm=None if agentic else llm_cols,
            agentic=llm_cols if agentic else None,
            agentic_steps=list(agentic_steps or []) if agentic else None,
            artifacts={
                "deterministic": "contract.deterministic.yaml",
                "single_llm": "contract.llm.yaml" if not agentic else "",
                "agentic": "contract.llm.yaml" if agentic else "",
            },
            domain="contract",
        )
        keys["result_variants.json"] = run_writer.write("result_variants.json", bundle["variants"])
        keys["result_comparison.json"] = run_writer.write("result_comparison.json", bundle["comparison"])
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("result comparison write failed: %s", exc)
    ref = {
        "active_version": active_version or c_llm_clean.get("version"),
        "source": "llm",
        "run_id": run_id,
        "det_version": det_version or c_det.get("version"),
    }
    keys["contract.active.ref.json"] = run_writer.write("contract.active.ref.json", ref)
    return {"keys": keys, "diff": diff}


def write_enrichment_meta_artifact(
    run_writer: RunOutputWriter,
    meta: dict,
) -> dict[str, str]:
    """Persist enrichment telemetry outside the contract spec."""
    key = run_writer.write("enrichment_meta.json", meta)
    return {"enrichment_meta.json": key}


def write_enrich_debug_log(
    run_writer: RunOutputWriter,
    *,
    table: str,
    run_id: str,
    provider: str,
    model: str,
    valid: bool,
    errors: Optional[list[str]] = None,
    warnings: Optional[list[str]] = None,
    prompt_chars: int = 0,
    response_chars: int = 0,
) -> dict[str, str]:
    """Write a per-table debug log for an enrichment run."""
    lines = [
        f"table={table}",
        f"run_id={run_id}",
        f"provider={provider}",
        f"model={model}",
        f"valid={valid}",
        f"prompt_chars={prompt_chars}",
        f"response_chars={response_chars}",
    ]
    for w in warnings or []:
        lines.append(f"warning: {w}")
    for e in errors or []:
        lines.append(f"error: {e}")
    name = f"{table}.debug.log"
    key = run_writer.write(name, "\n".join(lines) + "\n")
    return {name: key}


def append_lifecycle_provenance(
    metadata_store,
    table: str,
    *,
    workflow: str,
    run_id: str,
    run_uuid: str = "",
    active_source: str,
    engines: Optional[dict[str, Any]] = None,
    det_audit_version: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    prior_version: Optional[str] = None,
    llm_version: Optional[str] = None,
) -> None:
    """Append lifecycle telemetry fields to provenance (outside contract spec)."""
    from redibis.store.merger import make_provenance_entry

    entry = make_provenance_entry({}, workflow, run_id, run_uuid)
    entry["active_source"] = active_source
    if engines:
        entry["engines"] = engines
    if det_audit_version:
        entry["det_audit_version"] = det_audit_version
    if prompt_hash:
        entry["prompt_hash"] = prompt_hash
    if prior_version:
        entry["prior_version"] = prior_version
    if llm_version:
        entry["llm_version"] = llm_version
    metadata_store.append_provenance(table, entry)


def apply_llm_classification_decisions(
    store,
    table: str,
    pii_changes: list[dict],
    *,
    enriched_by: str = "",
    run_id: str = "",
) -> list[str]:
    """Route every LLM classification change through the PII decision overlay."""
    applied: list[str] = []
    for ch in merge_pii_overlay_changes(pii_changes):
        col = ch.get("column")
        if not col:
            continue
        status = ch.get("status")
        if status == "not_pii":
            store.set_pii_decision(
                table, col, "not_pii",
                decided_by=enriched_by,
                run_id=run_id,
            )
            applied.append(col)
        elif status == "pii":
            store.set_pii_decision(
                table, col, "pii",
                entity_type=ch.get("entity_type"),
                payload=ch.get("payload") or {},
                decided_by=enriched_by,
                run_id=run_id,
            )
            applied.append(col)
    return applied


def build_engine_set(
    *,
    pii_engines: str = "both",
    use_phonenumbers: bool = True,
    equation_mode: str = "independent",
    profiler_engine: str = "great_expectations",
) -> dict[str, Any]:
    """Compact engine-set record for provenance."""
    return {
        "pii_engines": pii_engines,
        "phonenumbers": "enabled" if use_phonenumbers else "disabled",
        "equation_mode": equation_mode,
        "profiler_engine": profiler_engine,
    }


def collect_pii_changes_from_contracts(c_det: dict, c_llm: dict) -> list[dict]:
    """Derive overlay decisions by comparing classification signals column-wise."""
    from redibis.contracts.privacy import column_is_pii, col_pii_engine

    changes: list[dict] = []
    det_cols = {p.get("name"): p for p in _iter_columns(c_det) if p.get("name")}
    llm_cols = {p.get("name"): p for p in _iter_columns(c_llm) if p.get("name")}
    for name in sorted(set(det_cols) | set(llm_cols)):
        det_p = det_cols.get(name, {})
        llm_p = llm_cols.get(name, {})
        det_pii = column_is_pii(det_p)
        llm_pii = column_is_pii(llm_p)
        if det_pii == llm_pii and (
            (col_pii_engine(det_p).get("entity_type") or None)
            == (col_pii_engine(llm_p).get("entity_type") or None)
        ) and (det_p.get("classification") or None) == (llm_p.get("classification") or None):
            continue
        if not llm_pii:
            changes.append({"column": name, "status": "not_pii"})
        else:
            payload: dict[str, Any] = {}
            if llm_p.get("classification"):
                payload["classification"] = llm_p["classification"]
            ce = col_pii_engine(llm_p)
            entity = ce.get("entity_type")
            if entity:
                payload["pii"] = {"detected": True, "entity_type": entity}
            if llm_p.get("privacy"):
                payload["privacy"] = copy.deepcopy(llm_p["privacy"])
            changes.append({
                "column": name,
                "status": "pii",
                "entity_type": entity,
                "payload": payload,
            })
    return changes


def merge_pii_overlay_changes(changes: list[dict]) -> list[dict]:
    """Deduplicate overlay rows by column (last writer wins)."""
    merged: dict[str, dict] = {}
    for ch in changes or []:
        col = ch.get("column")
        if col:
            merged[col] = ch
    return list(merged.values())


def resolve_enrichment_overlay_changes(
    c_det: dict,
    candidate: dict,
    *,
    explicit_changes: Optional[list[dict]] = None,
    delta: Optional[dict] = None,
) -> list[dict]:
    """Map LLM enrichment output → overlay decisions (promotions + demotions + entity)."""
    from_contract = collect_pii_changes_from_contracts(c_det, candidate)
    from_delta = parse_pii_changes_from_delta(c_det, delta or {})
    return merge_pii_overlay_changes(
        list(explicit_changes or []) + from_delta + from_contract,
    )


def parse_pii_changes_from_delta(c_det: dict, delta: dict) -> list[dict]:
    """Parse explicit ``pii`` blocks from the LLM JSON delta."""
    from redibis.contracts.privacy import column_is_pii, col_pii_engine

    det_cols = {p.get("name"): p for p in _iter_columns(c_det) if p.get("name")}
    changes: list[dict] = []
    for col, cd in (delta.get("columns") or {}).items():
        if not col or not isinstance(cd, dict):
            continue
        pii_edit = cd.get("pii")
        if not isinstance(pii_edit, dict):
            continue
        classification = (pii_edit.get("classification") or "").strip().lower()
        entity = pii_edit.get("entity_type")
        if classification == "none":
            changes.append({"column": col, "status": "not_pii"})
            continue
        if not classification and not entity:
            continue
        payload: dict[str, Any] = {}
        if classification:
            payload["classification"] = pii_edit["classification"]
        if entity:
            payload["pii"] = {"detected": True, "entity_type": entity}
        det_p = det_cols.get(col, {})
        if column_is_pii(det_p) and not entity:
            entity = col_pii_engine(det_p).get("entity_type")
        changes.append({
            "column": col,
            "status": "pii",
            "entity_type": entity,
            "payload": payload,
        })
    return changes


def _iter_columns(contract: dict):
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict) and prop.get("name"):
                yield prop
