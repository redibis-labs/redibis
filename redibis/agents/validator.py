"""Output validator (reporter) + smart retry for enrich/contract nodes (Phase 2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.agents.models import PipelineNode
from redibis.agents.registry import resolve_node_type
from redibis.agents.run_models import StepRecord, StepStatus
from redibis.agents.tool_runner import ToolContext, run_node_step
from redibis.config import RedibisConfig


_CRITIQUE_RETRY_KINDS = frozenset({"enrich", "contract", "contract_write"})
_REPAIR_BLOCK_RE = re.compile(
    r"(?:^|\n\n)# VALIDATOR REPAIR \(fix these issues in the enrichment delta\)\n[\s\S]*",
    re.MULTILINE,
)


@dataclass
class ValidationResult:
    ok: bool
    severity: str = "ok"  # ok | warn | error
    errors: list[str] = field(default_factory=list)
    critique: str = ""
    checked: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "severity": self.severity,
            "errors": list(self.errors),
            "critique": self.critique,
            "checked": dict(self.checked),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationResult":
        return cls(
            ok=bool(data.get("ok")),
            severity=str(data.get("severity") or "ok"),
            errors=list(data.get("errors") or []),
            critique=str(data.get("critique") or ""),
            checked=dict(data.get("checked") or {}),
        )


def _iter_columns(contract: dict):
    from redibis.agents.enrich_validation import iter_contract_columns

    yield from iter_contract_columns(contract)


def _safety_invariant_errors(contract: dict) -> list[str]:
    from redibis.agents.enrich_validation import enrich_safety_errors

    return enrich_safety_errors(contract)


def _validate_enrich_output(output: dict[str, Any], ctx: ToolContext, table: str) -> ValidationResult:
    checked: dict[str, Any] = {}
    errors: list[str] = []

    if output.get("skipped"):
        reason = str(output.get("reason") or "")
        sev = "warn" if reason else "ok"
        return ValidationResult(ok=True, severity=sev, checked={"skipped": True, "reason": reason})

    if not output.get("auto_written") and not output.get("deferred_write"):
        errors.append("enrich did not write active contract")
        checked["auto_written"] = False

    service_errors = list(output.get("validation_errors") or output.get("service_errors") or [])
    artifact_errors = list(output.get("artifact_odcs_errors") or [])
    safety_errors = list(output.get("safety_errors") or [])

    if output.get("validation_reported"):
        checked["validation_source"] = "service"
        checked["odcs_valid"] = not artifact_errors
        checked["safety_ok"] = not safety_errors
        errors.extend(artifact_errors)
        errors.extend(safety_errors)
        if output.get("valid") is False:
            errors.append("enrich reported valid=false")
            checked["service_valid"] = False
        if service_errors and output.get("valid") is False:
            checked["service_errors"] = service_errors
    elif ctx.contract_store is not None:
        contract = ctx.contract_store.get_active(table)
        checked["has_active"] = contract is not None
        checked["validation_source"] = "active_contract"
        if contract is None and output.get("auto_written"):
            errors.append(f"no active contract for {table!r}")
        elif contract is not None:
            from redibis.enrich.delta_schema import assert_odcs_v3_contract

            odcs_errors = assert_odcs_v3_contract(contract)
            checked["odcs_valid"] = not odcs_errors
            errors.extend(odcs_errors)
            safety_errors = _safety_invariant_errors(contract)
            checked["safety_ok"] = not safety_errors
            errors.extend(safety_errors)
        if output.get("valid") is False:
            errors.append("enrich reported valid=false")
            checked["service_valid"] = False

    missing_defs = int(output.get("missing_definitions") or 0)
    if missing_defs:
        errors.append(f"{missing_defs} column(s) missing business definitions")
        checked["missing_definitions"] = missing_defs

    severity = "ok"
    if errors:
        severity = "error"
    elif output.get("warnings") or output.get("enrichment_status") == "warn":
        severity = "warn"

    critique = _build_critique(errors, kind="enrich")
    return ValidationResult(
        ok=severity == "ok",
        severity=severity,
        errors=errors,
        critique=critique,
        checked=checked,
    )


def _validate_contract_output(output: dict[str, Any], ctx: ToolContext, table: str) -> ValidationResult:
    subs = output.get("sub_steps") or []
    enrich_sub = next((s for s in subs if s.get("kind") == "enrich"), None)
    if enrich_sub is not None:
        return _validate_enrich_output(enrich_sub, ctx, table)

    if output.get("skipped") or output.get("failed"):
        return ValidationResult(
            ok=not output.get("failed"),
            severity="error" if output.get("failed") else "warn",
            errors=[str(output.get("reason") or output.get("error") or "contract step skipped")],
            critique="Re-run contract step after fixing upstream scan failures.",
            checked={"skipped": bool(output.get("skipped"))},
        )

    errors: list[str] = []
    if not subs:
        errors.append("contract produced no sub_steps")
    for sub in subs:
        kind = sub.get("kind", "")
        if sub.get("failed"):
            errors.append(f"{kind} sub-step failed")
        elif kind in ("pii", "quality", "profile") and not sub.get("skipped"):
            if kind == "pii" and sub.get("columns_scanned", 0) == 0:
                errors.append("pii sub-step returned zero columns")
            if kind == "quality" and sub.get("expectations", 0) == 0 and not sub.get("skipped"):
                errors.append("quality sub-step returned zero expectations")

    severity = "error" if errors else "ok"
    return ValidationResult(
        ok=not errors,
        severity=severity,
        errors=errors,
        critique=_build_critique(errors, kind="contract"),
        checked={"sub_steps": len(subs)},
    )


def _validate_scan_output(output: dict[str, Any], *, kind: str) -> ValidationResult:
    if output.get("failed"):
        return ValidationResult(
            ok=False,
            severity="error",
            errors=[str(output.get("error") or "step failed")],
            checked={"failed": True},
        )
    if output.get("skipped"):
        return ValidationResult(
            ok=True,
            severity="warn",
            errors=[str(output.get("reason") or "skipped")],
            checked={"skipped": True},
        )
    errors: list[str] = []
    if kind == "profile_scan" and not output.get("columns") and "columns" in output:
        errors.append("profile returned zero columns")
    if kind == "pii_scan" and output.get("columns_scanned", 0) == 0:
        errors.append("pii scan returned zero columns")
    if kind == "quality_scan" and output.get("expectations", 0) == 0:
        errors.append("quality scan returned zero expectations")
    severity = "error" if errors else "ok"
    return ValidationResult(
        ok=not errors,
        severity=severity,
        errors=errors,
        checked={"kind": kind},
    )


def _build_critique(errors: list[str], *, kind: str) -> str:
    if not errors:
        return ""
    if kind == "enrich":
        return (
            "Fix the enrichment delta: ensure every column has a business definition, "
            "ODCS v3.x shape is valid, no telemetry keys in the contract, and PII columns "
            "have no quality blocks. Errors: " + "; ".join(errors[:5])
        )
    return f"Fix {kind} output: " + "; ".join(errors[:5])


def supports_critique_retry(node: PipelineNode) -> bool:
    if node.kind == "enrich":
        return True
    canonical = resolve_node_type(node.kind)
    if node.kind in _CRITIQUE_RETRY_KINDS or canonical in _CRITIQUE_RETRY_KINDS:
        if node.kind == "contract" or canonical == "contract":
            return bool(node.params.get("enrich"))
        return True
    return False


def validate_step(
    node: PipelineNode,
    output: dict[str, Any],
    ctx: ToolContext,
    *,
    table: str = "",
) -> ValidationResult:
    """Validate one node output — reporter only; never blocks the always-active write."""
    output = output or {}
    table = table or output.get("table") or ""
    kind = node.kind
    canonical = resolve_node_type(kind)

    if kind == "enrich" or (kind == "contract" and node.params.get("enrich")):
        if kind == "enrich":
            return _validate_enrich_output(output, ctx, table)
        return _validate_contract_output(output, ctx, table)

    if kind == "contract" or canonical == "contract":
        return _validate_contract_output(output, ctx, table)

    if kind == "contract_write":
        if output.get("skipped"):
            return ValidationResult(ok=True, severity="warn", checked={"skipped": True})
        if not output.get("merged") and not output.get("contract_version"):
            return ValidationResult(
                ok=False,
                severity="error",
                errors=["contract_write did not merge"],
                critique="Ensure approval is granted and partials exist before write.",
            )
        return ValidationResult(ok=True, checked={"merged": True})

    if kind in ("profile_scan", "profile"):
        return _validate_scan_output(output, kind="profile_scan")
    if kind in ("pii_scan",):
        return _validate_scan_output(output, kind="pii_scan")
    if kind in ("quality_scan",):
        return _validate_scan_output(output, kind="quality_scan")

    if kind == "catalog_push":
        if output.get("skipped"):
            return ValidationResult(ok=True, severity="warn", checked={"skipped": True})
        if output.get("dry_run") or output.get("entity_fqn") or output.get("pushed"):
            return ValidationResult(ok=True, checked={"publish": True})
        return ValidationResult(
            ok=False,
            severity="warn",
            errors=["catalog push not acknowledged"],
            checked={"publish": False},
        )

    if output.get("failed"):
        return ValidationResult(
            ok=False,
            severity="error",
            errors=[str(output.get("error") or "failed")],
        )
    return ValidationResult(ok=True, checked={"kind": kind})


def should_retry(step: StepRecord, result: ValidationResult, node: PipelineNode) -> bool:
    """Smart retry only for enrich/contract critique loops."""
    if result.ok or result.severity != "error":
        return False
    if not supports_critique_retry(node):
        return False
    return step.attempts <= step.max_retries


def apply_critique_to_params(node: PipelineNode, result: ValidationResult) -> PipelineNode:
    """Inject validator critique into enrich node params for the next attempt."""
    params = dict(node.params)
    prior = str(params.get("extra_instructions") or "")
    base = _REPAIR_BLOCK_RE.sub("", prior).strip()
    block = (
        "\n\n# VALIDATOR REPAIR (fix these issues in the enrichment delta)\n"
        f"{result.critique}\n"
        + "\n".join(f"- {e}" for e in result.errors[:8])
    )
    params["extra_instructions"] = (f"{base}{block}" if base else block).strip()
    params["repair_critique"] = result.critique
    return PipelineNode(
        id=node.id,
        kind=node.kind,
        label=node.label,
        params=params,
        position=node.position,
    )


def _default_max_retries(ctx: ToolContext) -> int:
    cfg = ctx._redibis_config()
    return max(0, int(getattr(cfg.agents, "max_retries", 2) or 2))


def _node_defers_enrich_write(node: PipelineNode) -> bool:
    """Standalone enrich nodes and contract+enrich share the write-once deferral path."""
    if node.kind == "enrich":
        return True
    canonical = resolve_node_type(node.kind)
    if (node.kind == "contract" or canonical == "contract") and node.params.get("enrich"):
        return True
    return False


def _defer_enrich_write(*, attempt: int, max_retries: int, node: PipelineNode) -> bool:
    """Defer active upsert while critique retries remain (write once on final attempt)."""
    if not _node_defers_enrich_write(node):
        return False
    if not supports_critique_retry(node) or max_retries <= 0:
        return False
    return attempt <= max_retries


def _node_with_write_active(node: PipelineNode, *, write_active: bool) -> PipelineNode:
    params = dict(node.params)
    params["write_active"] = write_active
    return PipelineNode(
        id=node.id,
        kind=node.kind,
        label=node.label,
        params=params,
        position=node.position,
    )


def _commit_pending_enrich(
    ctx: ToolContext,
    table: str,
    step: StepRecord,
    *,
    node: Optional[PipelineNode] = None,
) -> None:
    """Flush deferred enrich candidate to active/ after validation settles."""
    from redibis.agents.enrich_validation import enrich_output_validation_fields
    from redibis.agents.scan_bindings import get_table_state
    from redibis.enrich.service import enrichment_service_for_store

    state = get_table_state(ctx.run_states, table)
    pending = state.extras.get("pending_enrich")
    if pending is None or getattr(pending, "auto_written", False):
        return
    if ctx.contract_store is None:
        return
    committed = enrichment_service_for_store(ctx.contract_store).commit_active_write(pending)
    state.extras.pop("pending_enrich", None)
    meta = committed.enrichment_meta or {}
    enrich_out = {
        "table": committed.table,
        "auto_written": committed.auto_written,
        "deferred_write": False,
        "version_after": committed.version_after,
        "enrichment_status": committed.enrichment_status,
        "valid": committed.valid,
        "validation_errors": list(committed.errors),
        "artifact_odcs_errors": list(meta.get("artifact_odcs_errors") or []),
        "validation_reported": True,
        **enrich_output_validation_fields(
            result_valid=committed.valid,
            result_errors=list(committed.errors),
            enrichment_meta=meta,
            candidate=committed.candidate,
        ),
    }
    if node is not None and node.kind == "contract" and node.params.get("enrich"):
        out = dict(step.output or {})
        subs = list(out.get("sub_steps") or [])
        updated = False
        for idx, sub in enumerate(subs):
            if sub.get("kind") == "enrich":
                subs[idx] = {"kind": "enrich", **enrich_out}
                updated = True
                break
        if not updated:
            subs.append({"kind": "enrich", **enrich_out})
        out["sub_steps"] = subs
        step.output = out
    else:
        step.output = {**(step.output or {}), **enrich_out}


def _enrich_output_committed(output: dict[str, Any]) -> bool:
    if output.get("auto_written"):
        return True
    return any(
        sub.get("kind") == "enrich" and sub.get("auto_written")
        for sub in (output.get("sub_steps") or [])
    )


def _mark_degraded(step: StepRecord, result: ValidationResult) -> None:
    out = dict(step.output or {})
    out["degraded"] = True
    out["validation_critique"] = result.critique
    out["validation_errors"] = list(result.errors)
    if out.get("enrichment_status") in (None, "clean", "warn"):
        out["enrichment_status"] = "degraded"
    step.output = out


def _append_validation_to_report(ctx: ToolContext, table: str, step: StepRecord, result: ValidationResult) -> None:
    from redibis.agents.agent_report import append_validation_review_item

    append_validation_review_item(
        metadata_store=getattr(ctx.contract_store, "metadata", None) if ctx.contract_store else None,
        table=table,
        run_id=str((step.output or {}).get("run_id") or ""),
        step_id=step.step_id,
        node_kind=step.node_kind,
        result=result,
        attempts=step.attempts,
    )


def run_step_with_validation(
    node: PipelineNode,
    *,
    table: str,
    ctx: ToolContext,
    max_retries: Optional[int] = None,
) -> StepRecord:
    """Execute one node with bounded critique retry (enrich/contract only)."""
    max_r = max_retries if max_retries is not None else _default_max_retries(ctx)
    defer = _defer_enrich_write(attempt=1, max_retries=max_r, node=node)
    work_node = _node_with_write_active(node, write_active=not defer)
    step = run_node_step(work_node, table=table, ctx=ctx)
    step.max_retries = max_r
    step.attempts = 1

    vr = validate_step(work_node, step.output, ctx, table=table)
    step.validation = vr.to_dict()

    while should_retry(step, vr, node):
        work_node = apply_critique_to_params(work_node, vr)
        step.attempts += 1
        defer = _defer_enrich_write(attempt=step.attempts, max_retries=max_r, node=node)
        work_node = _node_with_write_active(work_node, write_active=not defer)
        retry = run_node_step(work_node, table=table, ctx=ctx)
        step.output = retry.output
        step.status = retry.status
        step.error = retry.error
        step.finished_at = retry.finished_at
        vr = validate_step(work_node, step.output, ctx, table=table)
        step.validation = vr.to_dict()

    if _node_defers_enrich_write(node):
        _commit_pending_enrich(ctx, table, step, node=node)
        if step.output and _enrich_output_committed(step.output):
            vr = validate_step(node, step.output, ctx, table=table)
            step.validation = vr.to_dict()

    if not vr.ok and supports_critique_retry(node):
        _mark_degraded(step, vr)
        if ctx.contract_store is not None:
            _append_validation_to_report(ctx, table, step, vr)

    if step.status == StepStatus.COMPLETED and not vr.ok and vr.severity == "error":
        if supports_critique_retry(node):
            pass  # degraded but not failed — always-active contract stands
        elif step.node_kind in ("profile_scan", "pii_scan", "quality_scan"):
            step.output = {**(step.output or {}), "degraded": True, "validation": vr.to_dict()}

    return step
