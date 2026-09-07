"""Generic load-by-run_id from named session roots."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from redibis.services.session.sources import get_source, list_sources
from redibis.services.session.state import (
    ScanSession,
    rehydrate_scan_session,
    session_manager,
)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_run_id(run_id: str) -> str:
    run_id = (run_id or "").strip()
    if not run_id or ".." in run_id or "/" in run_id or "\\" in run_id:
        raise ValueError(
            f"Invalid run_id {run_id!r}: use letters, digits, '_', '.', '-' only"
        )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            f"Invalid run_id {run_id!r}: use letters, digits, '_', '.', '-' only"
        )
    return run_id


def _summary(session: ScanSession, source: str) -> dict:
    session_dir = Path(session.data_path).parent
    has_data = (session_dir / "data.csv").is_file() or (session_dir / "session_data").is_file()
    return {
        "table_name": session.table_name,
        "status": session.status,
        "run_count": len(session.runs),
        "pii_detected_count": session.pii_detected_count,
        "quality_passed": session.quality_passed,
        "quality_total": session.quality_total,
        "created_at": session.created_at,
        "has_data": has_data,
        "source": source,
    }


def _remap_artifacts(session: ScanSession, session_dir: Path) -> None:
    """Rewrite stale absolute artifact paths to files under the loaded session folder."""
    session_dir = session_dir.resolve()
    for key, p in list(session.artifacts.items()):
        if not p:
            continue
        name = Path(str(p)).name
        if not name:
            continue
        found: Optional[Path] = None
        direct = session_dir / name
        if direct.is_file():
            found = direct
        else:
            for cand in session_dir.rglob(name):
                if cand.is_file():
                    found = cand
                    break
        if found is not None:
            session.artifacts[key] = str(found)

    for candidate in (session_dir / "data.csv", session_dir / "session_data"):
        if candidate.exists():
            session.data_path = str(candidate)
            break


def _live_session_for_folder(run_id: str, session_dir: Path) -> Optional[ScanSession]:
    """Return an in-memory session matching this folder id or its session.json id."""
    existing = session_manager.peek_session(run_id)
    if existing is not None:
        return existing
    state_file = session_dir / "session.json"
    if not state_file.is_file():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        sid = state.get("session_id") or run_id
        if sid != run_id:
            return session_manager.peek_session(sid)
    except Exception:
        pass
    return None


def _response(session: ScanSession, source: str, *, loaded: bool) -> dict:
    return {
        "session_id": session.session_id,
        "source": source,
        "loaded": loaded,
        "summary": _summary(session, source),
    }


def load_session(
    run_id: str,
    source: str = "scan",
    *,
    make_active: bool = True,
) -> dict:
    """Load a flushed session by folder id from a named source into SessionManager."""
    src = get_source(source)
    run_id = _safe_run_id(run_id)
    root = src.root()
    session_dir = root / run_id

    existing = _live_session_for_folder(run_id, session_dir)
    if existing is not None:
        return _response(
            existing,
            getattr(existing, "source", source) or source,
            loaded=False,
        )

    state_file = session_dir / "session.json"
    if not state_file.is_file():
        raise FileNotFoundError(f"No session {run_id!r} under source {source!r}")

    src.normalize(session_dir)
    session = rehydrate_scan_session(session_dir, root)
    if session is None:
        raise FileNotFoundError(f"Failed to rehydrate session {run_id!r}")

    dup = session_manager.peek_session(session.session_id)
    if dup is not None:
        return _response(dup, getattr(dup, "source", source) or source, loaded=False)

    _remap_artifacts(session, session_dir)
    session.source = source
    session.prepare_for_api()

    if make_active:
        session_manager.register(session)

    return _response(session, source, loaded=True)


def list_available(source: str = "scan") -> list[dict]:
    src = get_source(source)
    entries = session_manager.list_sessions(src.root())
    for entry in entries:
        entry["source"] = source
    return entries


def resolve_session(session_id: str) -> Optional[ScanSession]:
    """Return a live or rehydrated session, searching all registered sources."""
    existing = session_manager.peek_session(session_id)
    if existing is not None:
        return existing

    for src in list_sources():
        root = src.root()
        session_dir = root / session_id
        if not (session_dir / "session.json").is_file():
            continue
        try:
            result = load_session(session_id, src.name)
        except (FileNotFoundError, ValueError):
            continue
        return session_manager.get_session(result["session_id"])
    return None


def artifact_path_allowed(
    path: Path,
    *,
    session_dir: Optional[Path] = None,
    run_dir: Optional[Path] = None,
) -> bool:
    """True when *path* is under a source root and is not a restricted artifact."""
    try:
        resolved = path.resolve()
    except OSError:
        return False

    name = resolved.name.lower()
    posix = resolved.as_posix().lower()
    if posix.endswith(".raw.json") or name.endswith(".raw.json"):
        return False
    if name == "evidence_bundle.json":
        return False

    for allowed_dir in (session_dir, run_dir):
        if allowed_dir is None:
            continue
        try:
            resolved.relative_to(allowed_dir.resolve())
            return True
        except ValueError:
            pass

    for root in list_sources():
        try:
            resolved.relative_to(root.root().resolve())
            return True
        except ValueError:
            continue
    return False
