"""Per-run remediation report for always-active LLM enrichment (Phase 1 Ruling A)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.contracts.privacy import column_is_pii, col_pii_engine


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_enrichment_status(
    *,
    valid: bool,
    errors: list[str],
    warnings: list[str],
    delta_errors: list[str],
    scrubbed_cols: list[str],
    artifact_odcs_errors: Optional[list[str]] = None,
) -> str:
    """Map validation outcome to telemetry status (never blocks the write)."""
    artifact_odcs_errors = artifact_odcs_errors or []
    if not valid or errors or delta_errors or artifact_odcs_errors:
        return "degraded"
    if warnings or scrubbed_cols:
        return "warn"
    return "clean"


def _column_verdict(prop: dict) -> dict[str, Any]:
    pii = column_is_pii(prop)
    ce = col_pii_engine(prop)
    return {
        "is_pii": pii,
        "classification": prop.get("classification") or "",
        "entity_type": ce.get("entity_type") or prop.get("entity_type") or "",
        "confidence": ce.get("confidence"),
    }


def _iter_columns(contract: dict):
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict) and prop.get("name"):
                yield prop


def _columns_map(contract: dict) -> dict[str, dict]:
    return {str(p["name"]): p for p in _iter_columns(contract)}


def _column_flags(
    name: str,
    det: dict,
    llm: dict,
    *,
    diff_fields: list[dict],
    pii_reasons: dict[str, str],
    column_telemetry: dict[str, dict],
) -> dict[str, Any]:
    det_v = _column_verdict(det)
    llm_v = _column_verdict(llm)
    agreement = (
        det_v["is_pii"] == llm_v["is_pii"]
        and (det_v["entity_type"] or None) == (llm_v["entity_type"] or None)
        and (det_v["classification"] or None) == (llm_v["classification"] or None)
    )
    overrides = [f for f in diff_fields if f.get("column") == name and f.get("kind") == "override"]
    biz = llm.get("business") if isinstance(llm.get("business"), dict) else {}
    missing_definition = not (biz.get("definition") or "").strip()
    tel = column_telemetry.get(name) or {}
    confidence = tel.get("confidence") or det_v.get("confidence")
    low_confidence = confidence is not None and float(confidence) < 0.75

    flags: list[str] = []
    if not agreement:
        flags.append("divergence")
    if missing_definition:
        flags.append("missing_definition")
    if low_confidence:
        flags.append("low_confidence")
    if overrides and not pii_reasons.get(name):
        flags.append("unexplained_change")

    fix_hint = ""
    if missing_definition:
        fix_hint = "Add a plain-language business definition for this column."
    elif not agreement:
        fix_hint = "Review PII verdict divergence; add pii_reason if overriding automation."
    elif low_confidence:
        fix_hint = "Low detection confidence — confirm entity type and classification."
    elif overrides:
        fix_hint = "Review LLM field changes against deterministic baseline."

    return {
        "column": name,
        "deterministic": det_v,
        "llm": llm_v,
        "agreement": agreement,
        "confidence": confidence,
        "flags": flags,
        "fix_hint": fix_hint,
        "rerun_handle": {"table": None, "column": name, "action": "enrich"},
    }


def build_agent_report(
    *,
    table: str,
    run_id: str,
    c_det: dict,
    c_llm: dict,
    valid: bool,
    errors: list[str],
    warnings: list[str],
    enrichment_status: str,
    diff_report: dict,
    enrichment_meta: dict,
    agent_run_id: str = "",
) -> dict[str, Any]:
    """Build structured agent_report payload (Phase 2 extends with ValidationResults)."""
    det_cols = _columns_map(c_det)
    llm_cols = _columns_map(c_llm)
    all_names = sorted(set(det_cols) | set(llm_cols))
    diff_fields = list((diff_report or {}).get("fields") or [])
    pii_reasons = dict(enrichment_meta.get("pii_reasons") or {})
    scrubbed = list(enrichment_meta.get("pii_output_scrubbed") or [])
    delta_errors = list(enrichment_meta.get("delta_validation_errors") or [])
    artifact_errors = list(enrichment_meta.get("artifact_odcs_errors") or [])

    columns: list[dict[str, Any]] = []
    review_first: list[dict[str, Any]] = []

    for name in all_names:
        col = _column_flags(
            name,
            det_cols.get(name, {}),
            llm_cols.get(name, {}),
            diff_fields=diff_fields,
            pii_reasons=pii_reasons,
            column_telemetry={},
        )
        col["rerun_handle"]["table"] = table
        columns.append(col)
        if col["flags"]:
            review_first.append({
                "column": name,
                "priority": len(col["flags"]) + (0 if col["agreement"] else 2),
                "flags": col["flags"],
                "fix_hint": col["fix_hint"],
                "rerun_handle": {
                    "table": table,
                    "column": name,
                    "run_id": run_id,
                    "agent_run_id": agent_run_id,
                    "action": "enrich",
                },
            })

    review_first.sort(key=lambda x: (-x["priority"], x["column"]))

    odcs_status = "pass" if valid and not artifact_errors else (
        "warn" if valid else "fail"
    )

    return {
        "table": table,
        "run_id": run_id,
        "agent_run_id": agent_run_id,
        "generated_at": _utc_iso(),
        "enrichment_status": enrichment_status,
        "run_level": {
            "odcs_validity": odcs_status,
            "valid": valid,
            "errors": errors,
            "warnings": warnings,
            "delta_validation_errors": delta_errors,
            "artifact_odcs_errors": artifact_errors,
            "safety_strip": {
                "scrubbed_columns": scrubbed,
                "count": len(scrubbed),
            },
            "retried_count": int(enrichment_meta.get("retry_count") or 0),
            "degraded_count": sum(1 for c in columns if not c["agreement"] or c["flags"]),
            "prompt_hash": enrichment_meta.get("system_prompt_hash"),
            "provider": enrichment_meta.get("provider"),
            "model": enrichment_meta.get("model"),
            "version_after": enrichment_meta.get("version_after"),
            "det_version": enrichment_meta.get("det_version"),
        },
        "columns": columns,
        "review_first": review_first,
    }


def render_agent_report_md(report: dict[str, Any]) -> str:
    """Human-readable remediation guide."""
    lines = [
        f"# Agent report — {report.get('table')}",
        "",
        f"- **Run:** `{report.get('run_id')}`",
        f"- **Status:** `{report.get('enrichment_status')}`",
        f"- **Generated:** {report.get('generated_at')}",
        "",
        "## Run summary",
        "",
    ]
    run_level = report.get("run_level") or {}
    lines.append(f"- ODCS validity: **{run_level.get('odcs_validity')}**")
    lines.append(f"- Safety strip: {run_level.get('safety_strip', {}).get('count', 0)} column(s)")
    if run_level.get("errors"):
        lines.append("- Validation errors:")
        for err in run_level["errors"]:
            lines.append(f"  - {err}")
    if run_level.get("warnings"):
        lines.append("- Warnings:")
        for w in run_level["warnings"]:
            lines.append(f"  - {w}")

    review = report.get("review_first") or []
    if review:
        lines.extend(["", "## Review these first", ""])
        for item in review:
            lines.append(
                f"1. **{item['column']}** — {', '.join(item.get('flags') or [])}"
            )
            if item.get("fix_hint"):
                lines.append(f"   - Fix: {item['fix_hint']}")
            rh = item.get("rerun_handle") or {}
            lines.append(
                f"   - Rerun: `redibis enrich {rh.get('table')} --run-id {rh.get('run_id')}`"
            )

    lines.extend(["", "## Per-column", ""])
    for col in report.get("columns") or []:
        agree = "agree" if col.get("agreement") else "DIVERGE"
        flags = ", ".join(col.get("flags") or []) or "—"
        lines.append(f"- `{col['column']}` — {agree}; flags: {flags}")

    lines.append("")
    return "\n".join(lines)


def write_agent_report(
    *,
    report: dict[str, Any],
    run_writer: Any,
    metadata_store: Any = None,
    table: str = "",
) -> dict[str, str]:
    """Persist agent_report under the run folder + contract metadata telemetry."""
    keys: dict[str, str] = {}
    payload = json.dumps(report, indent=2, default=str)
    md = render_agent_report_md(report)
    keys["agent_report.json"] = run_writer.write("agent_report.json", report)
    keys["agent_report.md"] = run_writer.write("agent_report.md", md)

    if metadata_store is not None and table:
        current = metadata_store.get_telemetry(table)
        agent_reports = dict(current.get("agent_reports") or {})
        agent_reports[report.get("run_id") or ""] = {
            "enrichment_status": report.get("enrichment_status"),
            "generated_at": report.get("generated_at"),
            "run_id": report.get("run_id"),
            "agent_run_id": report.get("agent_run_id"),
            "review_count": len(report.get("review_first") or []),
            "artifact_keys": keys,
        }
        current["enrichment_status"] = report.get("enrichment_status")
        current["agent_reports"] = agent_reports
        current["last_agent_report"] = {
            "run_id": report.get("run_id"),
            "enrichment_status": report.get("enrichment_status"),
            "generated_at": report.get("generated_at"),
        }
        metadata_store.backend.put_json(
            metadata_store.bucket,
            metadata_store._telemetry_key(table),
            current,
        )
    return keys


def append_validation_review_item(
    *,
    metadata_store: Any,
    table: str,
    run_id: str,
    step_id: str,
    node_kind: str,
    result: Any,
    attempts: int,
) -> None:
    """Add validator exhaustion item to contract telemetry (not spec)."""
    if metadata_store is None or not table:
        return
    from dataclasses import is_dataclass

    if is_dataclass(result):
        payload = result.to_dict()
    elif isinstance(result, dict):
        payload = result
    else:
        payload = {"critique": str(result)}

    current = metadata_store.get_telemetry(table)
    items = list(current.get("validation_review") or [])
    items.append({
        "run_id": run_id,
        "step_id": step_id,
        "node_kind": node_kind,
        "attempts": attempts,
        "severity": payload.get("severity"),
        "errors": payload.get("errors") or [],
        "critique": payload.get("critique") or "",
        "rerun_handle": {
            "table": table,
            "run_id": run_id,
            "action": "enrich" if node_kind in ("enrich", "contract") else node_kind,
        },
    })
    current["validation_review"] = items[-20:]
    metadata_store.backend.put_json(
        metadata_store.bucket,
        metadata_store._telemetry_key(table),
        current,
    )


def merge_validation_into_report(
    report: dict[str, Any],
    *,
    validation: dict[str, Any],
    attempts: int,
) -> dict[str, Any]:
    """Extend an agent_report with validator critique (Phase 2)."""
    out = dict(report)
    run_level = dict(out.get("run_level") or {})
    run_level["retried_count"] = max(int(run_level.get("retried_count") or 0), attempts - 1)
    if not validation.get("ok"):
        run_level["validator_severity"] = validation.get("severity")
        run_level["validator_errors"] = validation.get("errors") or []
        item = {
            "column": "(run-level)",
            "priority": 10,
            "flags": ["validation_exhausted"],
            "fix_hint": validation.get("critique") or "Review validator errors and rerun enrich.",
            "rerun_handle": {
                "table": report.get("table"),
                "run_id": report.get("run_id"),
                "action": "enrich",
            },
        }
        review = list(out.get("review_first") or [])
        review.insert(0, item)
        out["review_first"] = review
        if out.get("enrichment_status") == "clean":
            out["enrichment_status"] = "degraded"
    out["run_level"] = run_level
    out["validation"] = validation
    return out
