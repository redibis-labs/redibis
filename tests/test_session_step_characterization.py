"""Characterization tests for web session step executors (Phase 6 safety net)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from redibis.services.session_service import (
    GlobalConfig,
    ScanSession,
    _build_scan_config,
    execute_pii_step,
    execute_profile_step,
    execute_quality_step,
    execute_unified_scan,
)
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.subcontract_store import SubcontractStore

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def _require_presidio():
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("great_expectations")


@pytest.fixture
def session_env(tmp_path):
    """Minimal session layout matching the web dashboard."""
    backend = LocalBackend(str(tmp_path / "storage"))
    store = ContractStore(backend, bucket="active-contracts")
    sub_store = SubcontractStore(backend)

    session_dir = tmp_path / "sess-001"
    session_dir.mkdir()
    data_path = session_dir / "session_data.csv"
    pd.DataFrame({
        "id": [1, 2, 3],
        "email": ["a@example.com", "b@example.com", "c@example.com"],
        "note": ["hello", "world", "text"],
    }).to_csv(data_path, index=False)

    session = ScanSession(
        session_id="sess-001",
        table_name="test.customers",
        data_path=str(data_path),
        common_config=GlobalConfig(
            scan_mode="both",
            pii_engines="regex",
            generate_ge_docs=False,
            automerge="none",
        ),
    )
    config = _build_scan_config(session.table_name, session.common_config, session_dir)
    return {
        "session": session,
        "config": config,
        "backend": backend,
        "store": store,
        "sub_store": sub_store,
        "session_dir": session_dir,
    }


def _snapshot(session: ScanSession, session_dir: Path) -> dict:
    artifact_files = {
        key: Path(path).name
        for key, path in session.artifacts.items()
        if path and Path(path).exists()
    }
    return {
        "status": session.status,
        "profiler_set": session.profiler is not None,
        "gatekeeper_set": session.quality_gatekeeper is not None,
        "artifact_keys": sorted(session.artifacts.keys()),
        "artifact_files": artifact_files,
        "quality_passed": session.quality_passed,
        "quality_total": session.quality_total,
        "pii_detected_count": session.pii_detected_count,
        "pii_detection_count": len(session.pii_detections),
        "sub_contracts": [(s.kind, s.status) for s in session.sub_contracts],
        "run_types": [r.run_type for r in session.runs],
        "run_statuses": [r.status for r in session.runs],
        "session_json": (session_dir / "session.json").exists(),
    }


def test_profile_step_characterization(session_env):
    session = session_env["session"]
    execute_profile_step(session, session_env["config"], session_env["store"])

    snap = _snapshot(session, session_env["session_dir"])
    assert snap["status"] == "profiling_complete"
    assert snap["profiler_set"] is True
    assert "interactive_review" in snap["artifact_keys"]
    assert "triage_report" in snap["artifact_keys"]
    assert Path(session.artifacts["interactive_review"]).is_file()
    assert Path(session.artifacts["triage_report"]).is_file()
    assert session.schema_contract_version is not None

    active = session_env["store"].get_active("test.customers")
    assert active is not None
    col_names = {p["name"] for p in active["schema"][0]["properties"]}
    assert col_names == {"id", "email", "note"}


def test_quality_step_characterization(session_env):
    session = session_env["session"]
    execute_profile_step(session, session_env["config"], session_env["store"])
    execute_quality_step(session, session_env["backend"], session_env["store"])

    snap = _snapshot(session, session_env["session_dir"])
    assert snap["status"] == "quality_complete"
    assert snap["gatekeeper_set"] is True
    assert "quality_report" in snap["artifact_keys"]
    assert "quality_contract" in snap["artifact_keys"]
    assert snap["quality_total"] >= 0
    assert ("quality", "staged") in snap["sub_contracts"]
    assert snap["run_types"] == ["scan_quality"]
    assert snap["run_statuses"] == ["complete"]
    assert snap["session_json"] is True
    assert session_env["sub_store"].get(
        "quality", "test.customers", session.runs[0].run_id,
    ) is not None


def test_rehydrate_restores_quality_results_from_run_dir(session_env):
    """session.json may omit heavy quality_results — reload from runs/<id>/."""
    session = session_env["session"]
    execute_profile_step(session, session_env["config"], session_env["store"])
    execute_quality_step(session, session_env["backend"], session_env["store"])
    session_dir = session_env["session_dir"]
    run_id = session.runs[0].run_id
    qpath = session_dir / "runs" / run_id / "quality_results.json"
    assert qpath.is_file()

    state = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    for run in state.get("runs") or []:
        run["quality_results"] = []
    (session_dir / "session.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    from redibis.services.session.state import rehydrate_scan_session

    session_env["session_manager"] = None
    reloaded = rehydrate_scan_session(session_dir, session_env["session_dir"].parent)
    assert reloaded is not None
    assert reloaded.quality_total >= 1
    assert reloaded.runs[0].quality_results
    assert "quality_contract" in reloaded.artifacts


def test_pii_step_characterization(session_env):
    session = session_env["session"]
    session.config = session_env["config"]
    execute_pii_step(session, session_env["backend"], session_env["store"])

    snap = _snapshot(session, session_env["session_dir"])
    assert snap["status"] == "pii_complete"
    assert snap["pii_detection_count"] == 3
    assert "pii_regex_review" in snap["artifact_keys"]
    assert "pii_detections" in snap["artifact_keys"]
    assert "pii_contract" in snap["artifact_keys"]
    assert ("pii", "staged") in snap["sub_contracts"]
    assert session_env["sub_store"].get(
        "pii", "test.customers", session.runs[0].run_id,
    ) is not None


def test_unified_scan_both_defers_report_until_complete(session_env, monkeypatch):
    """Quality must not finalize (status/report) while PII still runs in mode=both."""
    session = session_env["session"]
    statuses: list[str] = []

    original_set_status = session.set_status

    def _track_status(status: str) -> None:
        statuses.append(status)
        original_set_status(status)

    monkeypatch.setattr(session, "set_status", _track_status)

    report_calls: list[str] = []
    from redibis.services.session import steps as session_steps

    original_generate = session_steps._generate_run_report

    def _track_report(sess: ScanSession) -> None:
        report_calls.append(sess.status)
        original_generate(sess)

    monkeypatch.setattr(session_steps, "_generate_run_report", _track_report)

    execute_unified_scan(
        session, session_env["backend"], session_env["store"],
    )

    assert session.status == "scan_complete"
    assert "quality_complete" not in statuses
    assert "pii_complete" not in statuses
    assert statuses.count("scan_complete") == 1
    assert report_calls == ["scan_complete"]
    assert "run_report" in session.artifacts
    assert session.pii_detected_count >= 0
    assert session.quality_total >= 0
