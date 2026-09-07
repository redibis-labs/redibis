"""FIX 4: scan jobs always reach a terminal session status."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from redibis.services.session.config import GlobalConfig
from redibis.services.session.state import ScanSession
from redibis.webapp import jobs


@pytest.fixture(autouse=True)
def _reset_jobs(monkeypatch):
    monkeypatch.setenv("REDIBIS_SCAN_WORKERS", "1")
    jobs._inflight = 0


def _session(tmp_path, status: str = "running") -> ScanSession:
    session = ScanSession(
        session_id="s1",
        table_name="db.t",
        data_path=str(tmp_path / "data.csv"),
        common_config=GlobalConfig(),
    )
    session.set_status(status)
    session.persist_to_disk = MagicMock()
    return session


def test_job_marks_complete_when_fn_leaves_running(tmp_path):
    session = _session(tmp_path, "running")

    def noop():
        pass

    jobs.submit_scan_job(noop, session=session).result(timeout=5)
    assert session.status == "complete"


def test_job_marks_failed_on_exception(tmp_path):
    session = _session(tmp_path, "running")

    def boom():
        raise RuntimeError("fail")

    jobs.submit_scan_job(boom, session=session).result(timeout=5)
    assert session.status == "failed"


def test_job_preserves_terminal_status_from_fn(tmp_path):
    session = _session(tmp_path, "running")

    def done():
        session.set_status("scan_complete")

    jobs.submit_scan_job(done, session=session).result(timeout=5)
    assert session.status == "scan_complete"
