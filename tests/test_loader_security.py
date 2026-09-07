"""Security tests for session loader."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_loader_sec_storage")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_loader_sec_output")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_loader_sec_configs")

from redibis.services.session.loader import _safe_run_id, artifact_path_allowed


@pytest.mark.parametrize("bad", ["../etc", "foo/bar", "..", "/abs", "a\\b", ""])
def test_safe_run_id_rejects_traversal(bad):
    with pytest.raises(ValueError):
        _safe_run_id(bad)


def test_safe_run_id_accepts_valid():
    assert _safe_run_id("abc-123_456.test") == "abc-123_456.test"


@pytest.fixture
def client():
    from redibis.webapp.backend import app
    return TestClient(app)


def test_artifact_endpoint_rejects_outside_root(client, tmp_path, monkeypatch):
    from redibis.services.session.state import ScanSession, session_manager
    from redibis.services.session.config import GlobalConfig

    scan_root = tmp_path / "scan_output"
    scan_root.mkdir()
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))

    session_id = "sec-artifact-001"
    session_dir = scan_root / session_id
    session_dir.mkdir()
    data_path = session_dir / "data.csv"
    data_path.write_text("x\n1\n", encoding="utf-8")

    outside = tmp_path / "outside.html"
    outside.write_text("<html>secret</html>", encoding="utf-8")

    session = ScanSession(
        session_id=session_id,
        table_name="t.x",
        data_path=str(data_path),
        common_config=GlobalConfig(),
    )
    session.artifacts = {"evil": str(outside)}
    session.persist_to_disk()
    session_manager.register(session)

    r = client.get(f"/api/sessions/{session_id}/artifacts/evil")
    assert r.status_code == 404


def test_artifact_served_under_session_dir_when_source_root_moved(client, tmp_path, monkeypatch):
    from redibis.services.session.state import ScanSession, session_manager
    from redibis.services.session.config import GlobalConfig

    scan_root = tmp_path / "scan_output"
    scan_root.mkdir()
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))

    session_id = "mask-artifact-001"
    session_dir = scan_root / session_id
    session_dir.mkdir()
    data_path = session_dir / "data.csv"
    data_path.write_text("x\n1\n", encoding="utf-8")

    report = session_dir / "masked" / "export.csv"
    report.parent.mkdir()
    report.write_text("masked\n", encoding="utf-8")

    session = ScanSession(
        session_id=session_id,
        table_name="t.x",
        data_path=str(data_path),
        common_config=GlobalConfig(),
    )
    session.artifacts = {"masked_export": str(report)}
    session_manager.register(session)

    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "other_scan_root"))

    r = client.get(f"/api/sessions/{session_id}/artifacts/masked_export")
    assert r.status_code == 200


def test_artifact_path_allowed_under_source_root(tmp_path, monkeypatch):
    scan_root = tmp_path / "scan_output"
    scan_root.mkdir()
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))
    inside = scan_root / "sess" / "report.html"
    inside.parent.mkdir()
    inside.write_text("ok", encoding="utf-8")
    assert artifact_path_allowed(inside) is True
    outside = tmp_path / "elsewhere.html"
    outside.write_text("no", encoding="utf-8")
    assert artifact_path_allowed(outside) is False


def test_artifact_path_allowed_under_session_dir_outside_source_root(tmp_path, monkeypatch):
    """Masked exports under session_dir remain servable when SCAN_OUTPUT_DIR moved."""
    scan_root = tmp_path / "scan_output"
    scan_root.mkdir()
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))

    session_dir = tmp_path / "legacy_sessions" / "sess-1"
    masked = session_dir / "masked" / "table_mask_001.csv"
    masked.parent.mkdir(parents=True)
    masked.write_text("x\n1\n", encoding="utf-8")

    assert artifact_path_allowed(masked, session_dir=session_dir) is True
    assert artifact_path_allowed(masked) is False


def test_artifact_path_allowed_under_run_dir_outside_source_root(tmp_path):
    run_dir = tmp_path / "legacy_runs" / "run-1"
    artifact = run_dir / "ge_report" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html>ok</html>", encoding="utf-8")

    assert artifact_path_allowed(artifact, run_dir=run_dir) is True
    assert artifact_path_allowed(artifact) is False
