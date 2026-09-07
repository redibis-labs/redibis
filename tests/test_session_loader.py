"""Tests for load_session and list_available."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_loader_storage")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_loader_configs")


@pytest.fixture
def scan_root(tmp_path, monkeypatch):
    root = tmp_path / "scan_output"
    root.mkdir()
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(root))
    return root


@pytest.fixture
def flushed_session(scan_root):
    from redibis.services.session.state import ScanSession, session_manager
    from redibis.services.session.config import GlobalConfig

    session_id = "test-run-001"
    session_dir = scan_root / session_id
    session_dir.mkdir(parents=True)
    data_path = session_dir / "data.csv"
    data_path.write_text("email,age\na@b.com,30\n", encoding="utf-8")

    session = ScanSession(
        session_id=session_id,
        table_name="telecom.customers",
        data_path=str(data_path),
        common_config=GlobalConfig(),
    )
    session.status = "scan_complete"
    session.pii_detected_count = 2
    session.quality_passed = 3
    session.quality_total = 4
    session.artifacts = {"quality_report": "/old/host/quality-report-abc.html"}
    (session_dir / "quality-report-abc.html").write_text("<html>ok</html>", encoding="utf-8")
    session.persist_to_disk()
    session_manager.delete_session(session_id)
    return session_id


def test_load_session_from_scan_root(scan_root, flushed_session):
    from redibis.services.session import loader as load_module
    from redibis.services.session.state import session_manager

    result = load_module.load_session(flushed_session, "scan")
    assert result["loaded"] is True
    assert result["session_id"] == flushed_session
    assert result["source"] == "scan"
    assert result["summary"]["table_name"] == "telecom.customers"
    assert result["summary"]["has_data"] is True
    assert result["summary"]["pii_detected_count"] == 2

    live = session_manager.get_session(flushed_session)
    assert live is not None
    assert live.table_name == "telecom.customers"
    assert len(live.runs) >= 0
    assert live.approved is not None


def test_load_session_idempotent(scan_root, flushed_session):
    from redibis.services.session import loader as load_module

    first = load_module.load_session(flushed_session, "scan")
    second = load_module.load_session(flushed_session, "scan")
    assert first["loaded"] is True
    assert second["loaded"] is False
    assert second["session_id"] == flushed_session


def test_load_session_dedupes_by_session_json_id(scan_root):
    """Folder name may differ from session_id inside session.json."""
    from redibis.services.session import loader as load_module
    from redibis.services.session.config import GlobalConfig
    from redibis.services.session.state import ScanSession, session_manager

    folder_id = "folder-alias-001"
    canonical_id = "canonical-uuid-9999"
    session_dir = scan_root / folder_id
    session_dir.mkdir(parents=True)
    data_path = session_dir / "data.csv"
    data_path.write_text("x\n1\n", encoding="utf-8")

    session = ScanSession(
        session_id=canonical_id,
        table_name="t.x",
        data_path=str(data_path),
        common_config=GlobalConfig(),
    )
    session.persist_to_disk()
    session_manager.register(session)

    result = load_module.load_session(folder_id, "scan")
    assert result["loaded"] is False
    assert result["session_id"] == canonical_id


def test_list_available(scan_root, flushed_session):
    from redibis.services.session import loader as load_module

    rows = load_module.list_available("scan")
    ids = {r["session_id"] for r in rows}
    assert flushed_session in ids
    assert all(r.get("source") == "scan" for r in rows)
