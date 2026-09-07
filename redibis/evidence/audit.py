"""Steward-access audit records for restricted evidence reads."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from redibis.evidence.persist import chmod_restricted_dir, chmod_restricted_file, ensure_restricted_dir
from redibis.evidence.spool import audit_log_path

log = logging.getLogger(__name__)


class RestrictedAccessError(Exception):
    """Steward policy refused a restricted read."""

    def __init__(self, message: str, *, code: int = 2):
        super().__init__(message)
        self.code = code


def restricted_read_record(
    *,
    actor: str,
    reason: str,
    call_id: str = "",
    run_id: str = "",
    table: str = "",
    outcome: str = "ok",
    error: str = "",
    role: str = "data_steward",
) -> dict[str, Any]:
    return {
        "kind": "redibis.restricted_read",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": str(actor or ""),
        "role": str(role or "data_steward"),
        "reason": str(reason or ""),
        "call_id": str(call_id or ""),
        "run_id": str(run_id or ""),
        "table": str(table or ""),
        "outcome": str(outcome or "ok"),
        "error": str(error or ""),
    }


def authorize_restricted_read(
    *,
    actor: str = "",
    reason: str = "",
    role: str = "",
    policy: Any = None,
) -> str:
    """Validate actor/reason/role against ``EvidenceAccessPolicy``.

    Returns the effective steward role. Raises ``RestrictedAccessError``.
    """
    require_actor = True
    require_reason = True
    steward_role = "data_steward"
    if policy is not None:
        require_actor = bool(getattr(policy, "require_actor", True))
        require_reason = bool(getattr(policy, "require_reason", True))
        steward_role = str(getattr(policy, "steward_role", None) or "data_steward")
    actor_s = (actor or "").strip()
    reason_s = (reason or "").strip()
    role_s = (role or "").strip() or steward_role
    if require_actor and not actor_s:
        raise RestrictedAccessError("restricted read requires --actor")
    if require_reason and not reason_s:
        raise RestrictedAccessError("restricted read requires --reason")
    if role_s != steward_role:
        raise RestrictedAccessError(
            f"role {role_s!r} is not authorized (requires {steward_role!r})"
        )
    return role_s


def append_restricted_read(
    spool_dir: Path | str | None,
    record: dict[str, Any],
) -> Optional[Path]:
    """Append one JSONL audit row. Returns the path, or None on I/O failure."""
    path = audit_log_path(spool_dir)
    try:
        ensure_restricted_dir(path.parent)
        chmod_restricted_dir(path.parent)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        chmod_restricted_file(path)
        return path
    except OSError as exc:
        log.warning("restricted-read audit write failed path=%s: %s", path, exc)
        return None
