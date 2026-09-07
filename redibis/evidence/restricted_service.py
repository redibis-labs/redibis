"""Library entry for reading one restricted (raw) LLM evidence record.

REST/CLI-agnostic core shared by the ``/api/evidence/{table}/restricted/llm``
route (``redibis.webapp.review_routes``) and mirrors ``redibis scan evidence
llm --raw`` (``redibis.cli.evidence_cmd._run_evidence_llm``, the canonical,
more feature-complete CLI implementation — this covers the one-call lookup the
run explorer's "view exact evidence" action needs).

Every read is role-gated (``EvidenceAccessPolicy``), requires an actor and a
reason, and is appended to the governed local audit log
(``redibis.evidence.audit``) — never silently served.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from redibis.evidence.audit import (
    RestrictedAccessError,
    append_restricted_read,
    authorize_restricted_read,
    restricted_read_record,
)

__all__ = ["RestrictedAccessError", "read_restricted_llm_call"]


def _configured_spool_and_policy(output_dir: Optional[Path] = None):
    from redibis.evidence.spool import DEFAULT_SPOOL_DIR
    from redibis.telemetry.llm_evidence import resolve_spool_dir

    try:
        from redibis.config import RedibisConfig

        cfg = RedibisConfig.load()
    except Exception:
        cfg = None
    spool = resolve_spool_dir(cfg)
    policy = getattr(getattr(cfg, "evidence", None), "access", None) if cfg is not None else None
    if output_dir is not None:
        try:
            using_default = Path(spool).resolve() == Path(DEFAULT_SPOOL_DIR).resolve()
        except OSError:
            using_default = Path(spool) == Path(DEFAULT_SPOOL_DIR)
        if using_default:
            spool = Path(output_dir) / "_restricted_evidence"
    return spool, policy


def _find_run_dir(output_dir: Path, run_id: str) -> Optional[Path]:
    direct = Path(output_dir) / run_id
    if direct.is_dir():
        return direct
    for path in Path(output_dir).rglob(run_id):
        if path.is_dir() and (
            (path / "evidence_manifest.json").is_file()
            or (path / "evidence_bundle.json").is_file()
        ):
            return path
    return None


def read_restricted_llm_call(
    *,
    table: str,
    run_id: str,
    call_id: str,
    actor: str,
    reason: str,
    role: str = "",
    output_dirs: Optional[list[Path]] = None,
) -> dict[str, Any]:
    """Return the raw payload for one LLM call, enforcing policy + audit.

    Raises ``RestrictedAccessError`` when the actor/reason/role fail policy,
    or when the run/call cannot be located locally (evidence never leaves the
    node it was recorded on — see ``docs/EVIDENCE_STORE.md``).
    """
    from redibis.evidence.spool import llm_spool_dir

    dirs = [Path(d) for d in (output_dirs or [])]
    cfg_spool, policy = _configured_spool_and_policy(dirs[0] if dirs else None)
    audit_enabled = True if policy is None else bool(getattr(policy, "audit_enabled", True))

    effective_role = authorize_restricted_read(actor=actor, reason=reason, role=role, policy=policy)

    def _audit(outcome: str, error: str = "") -> None:
        path = append_restricted_read(cfg_spool, restricted_read_record(
            actor=actor, reason=reason, call_id=call_id, run_id=run_id,
            table=table, outcome=outcome, error=error, role=effective_role,
        ))
        if audit_enabled and path is None:
            raise RestrictedAccessError("audit write failed; refusing restricted read", code=1)

    run_dir: Optional[Path] = None
    for d in dirs:
        run_dir = _find_run_dir(d, run_id)
        if run_dir is not None:
            break

    search_roots = [p for p in (run_dir, llm_spool_dir(cfg_spool, run_id).parent if run_id else None) if p]
    index = None
    index_root: Optional[Path] = None
    for root in search_roots:
        idx = Path(root) / "llm_calls" / "index.json"
        if idx.is_file():
            index = json.loads(idx.read_text(encoding="utf-8"))
            index_root = Path(root)
            break
    if index is None:
        _audit("missing", "no llm_calls index found locally")
        raise RestrictedAccessError(f"no local LLM call index for run {run_id!r}", code=1)

    for entry in index.get("calls") or []:
        if entry.get("call_id") != call_id:
            continue
        rel = entry.get("raw") or entry.get("spool") or ""
        path = None
        if rel:
            cand = Path(rel) if Path(rel).is_absolute() else (index_root / rel if index_root else None)
            if cand is not None and cand.is_file():
                path = cand
        if path is None:
            spool_dir = llm_spool_dir(cfg_spool, run_id)
            matches = list(spool_dir.glob(f"*-{call_id}.raw.json")) if spool_dir.is_dir() else []
            if matches:
                path = matches[0]
        if path is None or not path.is_file():
            _audit("missing", "restricted file not found locally")
            raise RestrictedAccessError(f"no local restricted file for call {call_id!r}", code=1)
        _audit("ok")
        return json.loads(path.read_text(encoding="utf-8"))

    _audit("missing", "call_id not found in index")
    raise RestrictedAccessError(f"call {call_id!r} not found in run {run_id!r}", code=1)
