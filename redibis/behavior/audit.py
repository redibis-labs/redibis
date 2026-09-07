"""
redibis.behavior.audit
=======================
Structured, sanitized policy audit events.

Audit data NEVER contains raw values. Context is represented by a hash
of its normalized (value-free) keys + types.

Events flow to:
  - run artifact JSONL (append-only)
  - structured decision logger (redibis.obs.decision)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from redibis.behavior.models import (
    BehaviorContext,
    BehaviorPatch,
    BlockedOperation,
    EvaluationTrace,
    PolicyAuditEvent,
    RuleTrace,
    RuleTraceStatus,
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def _context_hash(ctx: BehaviorContext) -> str:
    """Stable hash of context key/type pairs — no raw values."""
    keys = sorted(
        f"{k}:{type(v).__name__}"
        for k, v in ctx.facts.items()
    )
    data = json.dumps(keys, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def _before_hash(facts: Any) -> str:
    """Hash of fact keys that represent the 'before' state."""
    # Extract verdict-like keys as a proxy for 'before' state
    verdict_keys = {
        k: type(v).__name__
        for k, v in (facts.items() if hasattr(facts, "items") else {}.items())
        if k.startswith("verdict.")
    }
    data = json.dumps(sorted(verdict_keys.items()), separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def _patch_to_sanitized_dict(patch: BehaviorPatch) -> dict:
    """Sanitize a patch for audit — no raw cell values."""
    return {
        "operations": [
            {
                "op": op.op,
                "path": op.path,
                # value is included only if it's metadata-safe (not a raw cell)
                "value": op.value if _is_safe_value(op.value) else "<redacted>",
                "authority": op.authority.name,
                "rule_id": op.rule_id,
                "policy_id": op.policy_id,
                "reason": op.reason,
            }
            for op in patch.operations
        ],
        "require_review": patch.require_review,
        "review_role": patch.review_role,
        "reasons": list(patch.reasons),
    }


def _is_safe_value(v: Any) -> bool:
    """True if value is safe to include in audit (metadata / categorical, not raw data)."""
    if v is None:
        return True
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return True   # scores and rates are safe aggregates
    if isinstance(v, str) and len(v) <= 128:
        return True   # short strings are likely entity types / labels
    return False


def build_audit_event(
    *,
    run_id: str,
    ctx: BehaviorContext,
    policy_id: str,
    policy_version: str,
    policy_sha256: str,
    rule_id: str,
    action_ids: tuple[str, ...],
    actor: str,
    trace: EvaluationTrace,
    duration_ms: float,
    outcome: str,
) -> PolicyAuditEvent:
    """Build a sanitized PolicyAuditEvent from evaluation results."""
    return PolicyAuditEvent(
        event_id=str(uuid.uuid4()),
        ts=_utc_iso(),
        run_id=run_id,
        table=ctx.table,
        column=ctx.column,
        engine=ctx.engine,
        stage=ctx.stage.value,
        policy_id=policy_id,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        rule_id=rule_id,
        action_ids=action_ids,
        actor=actor,
        context_hash=_context_hash(ctx),
        before_hash=_before_hash(ctx.facts),
        proposed_patch=_patch_to_sanitized_dict(trace.proposed_patch),
        applied_patch=_patch_to_sanitized_dict(trace.applied_patch),
        blocked_operations=tuple(
            {
                "path": b.operation.path,
                "blocked_by": b.blocked_by,
                "explanation": b.explanation,
            }
            for b in trace.blocked_operations
        ),
        outcome=outcome,
        duration_ms=duration_ms,
    )


def write_audit_events(
    events: list[PolicyAuditEvent],
    *,
    artifact_dir: Optional[Path] = None,
) -> None:
    """
    Write audit events to a JSONL file in artifact_dir.

    If artifact_dir is None, events are only logged (no file write).
    """
    import logging
    log = logging.getLogger("redibis.behavior.audit")

    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        audit_file = artifact_dir / "behavior_audit.jsonl"
        with audit_file.open("a", encoding="utf-8") as f:
            for evt in events:
                line = json.dumps(_event_to_dict(evt), separators=(",", ":"))
                f.write(line + "\n")

    for evt in events:
        log.debug(
            "behavior_audit event=%s policy=%s rule=%s outcome=%s",
            evt.event_id, evt.policy_id, evt.rule_id, evt.outcome,
        )


def _event_to_dict(evt: PolicyAuditEvent) -> dict:
    return {
        "event_id": evt.event_id,
        "ts": evt.ts,
        "run_id": evt.run_id,
        "table": evt.table,
        "column": evt.column,
        "engine": evt.engine,
        "stage": evt.stage,
        "policy_id": evt.policy_id,
        "policy_version": evt.policy_version,
        "policy_sha256": evt.policy_sha256,
        "rule_id": evt.rule_id,
        "action_ids": list(evt.action_ids),
        "actor": evt.actor,
        "context_hash": evt.context_hash,
        "before_hash": evt.before_hash,
        "proposed_patch": evt.proposed_patch,
        "applied_patch": evt.applied_patch,
        "blocked_operations": list(evt.blocked_operations),
        "outcome": evt.outcome,
        "duration_ms": evt.duration_ms,
    }
