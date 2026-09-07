"""Phase 4: bounded scan job executor."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")


def test_failed_job_sets_terminal_status_and_persists(monkeypatch, tmp_path):
    from redibis.webapp import jobs
    from redibis.services.session.state import ScanSession
    from redibis.services.session.config import GlobalConfig

    monkeypatch.setenv("REDIBIS_SCAN_WORKERS", "1")
    jobs._inflight = 0

    session = ScanSession(
        session_id="s1",
        table_name="db.t",
        data_path=str(tmp_path / "data.csv"),
        common_config=GlobalConfig(),
    )
    session.set_status("running")
    persist = MagicMock()
    session.persist_to_disk = persist

    def boom():
        raise RuntimeError("scan blew up")

    fut = jobs.submit_scan_job(boom, session=session)
    fut.result(timeout=5)
    assert session.status == "failed"
    persist.assert_called()


def test_health_responsive_while_jobs_running(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_output"))
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("REDIBIS_SCAN_WORKERS", "1")

    from redibis.webapp import jobs

    jobs._inflight = 0
    barrier = {"go": False}

    def slow():
        while not barrier["go"]:
            time.sleep(0.05)

    jobs.submit_scan_job(slow)
    jobs.submit_scan_job(slow)

    from redibis.webapp.backend import app

    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 200
    barrier["go"] = True
