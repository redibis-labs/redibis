"""Phase 5: bounded session store with TTL and disk rehydration."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from redibis.services.session.config import GlobalConfig
from redibis.services.session.state import ScanSession, SessionManager


def _make_session(sid: str, root: Path) -> ScanSession:
    run_dir = root / sid
    run_dir.mkdir(parents=True, exist_ok=True)
    data_path = run_dir / "data.csv"
    data_path.write_text("a\n1\n", encoding="utf-8")
    session = ScanSession(
        session_id=sid,
        table_name="db.t",
        data_path=str(data_path),
        common_config=GlobalConfig(),
    )
    session.persist_to_disk()
    return session


def test_eviction_to_max_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path))
    mgr = SessionManager(max_sessions=3, ttl_seconds=99999)
    for i in range(8):
        sid = f"s{i}"
        session = _make_session(sid, tmp_path)
        mgr.register(session)
    assert len(mgr._sessions) == 3


def test_ttl_eviction(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path))
    mgr = SessionManager(max_sessions=50, ttl_seconds=1)
    session = _make_session("old", tmp_path)
    session.last_used = time.time() - 10
    mgr._sessions["old"] = session
    mgr._evict()
    assert "old" not in mgr._sessions


def test_rehydrate_after_eviction(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path))
    mgr = SessionManager(max_sessions=1, ttl_seconds=99999)
    s1 = _make_session("first", tmp_path)
    s2 = _make_session("second", tmp_path)
    mgr.register(s1)
    mgr.register(s2)
    assert "first" not in mgr._sessions
    loaded = mgr.get_session("first")
    assert loaded is not None
    assert loaded.session_id == "first"
