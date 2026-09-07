"""Artifact path remapping on session load."""

from __future__ import annotations

from pathlib import Path

import pytest

from redibis.services.session.config import GlobalConfig
from redibis.services.session.loader import _remap_artifacts
from redibis.services.session.state import ScanSession


def test_remap_artifacts_stale_absolute_paths(tmp_path):
    session_dir = tmp_path / "run-abc"
    session_dir.mkdir()
    report = session_dir / "quality-report-xyz.html"
    report.write_text("<html>report</html>", encoding="utf-8")

    session = ScanSession(
        session_id="run-abc",
        table_name="db.t",
        data_path=str(session_dir / "missing.csv"),
        common_config=GlobalConfig(),
    )
    session.artifacts = {"quality_report": "/other/container/quality-report-xyz.html"}

    _remap_artifacts(session, session_dir)

    assert Path(session.artifacts["quality_report"]).resolve() == report.resolve()
    assert session.data_path == str(session_dir / "missing.csv")

    (session_dir / "data.csv").write_text("a\n1\n", encoding="utf-8")
    _remap_artifacts(session, session_dir)
    assert session.data_path == str(session_dir / "data.csv")
