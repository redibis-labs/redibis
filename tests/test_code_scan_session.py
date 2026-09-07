"""Tests for CodeScanSession — session_id/run_id layout and auto_write to contract store."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from redibis.models import PIIDetection
from redibis.services.code_scan_session import CodeScanSession, _resolve_automerge
from redibis.services.scan_service import ScanResult
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.subcontract_store import SubcontractStore
from tests.contract_helpers import col_pii_engine


@pytest.fixture
def backend(tmp_path):
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def store(backend):
    return ContractStore(backend, bucket="active-contracts")


@pytest.fixture
def sub_store(backend):
    return SubcontractStore(backend, pii_bucket="pii-contracts",
                            quality_bucket="quality-contracts")


@pytest.fixture
def sample_df():
    return pd.DataFrame({
        "phone": ["+201012345678", "+201112345678"],
        "city": ["Cairo", "Alex"],
    })


@pytest.fixture
def mock_pii_detections():
    return [
        PIIDetection(
            column="phone", detected=True, entity_type="PHONE_NUMBER",
            confidence=0.92, presidio_score=0.88, gliner_score=0.90,
        ),
        PIIDetection(
            column="city", detected=False, decision_path="skipped_by_triage",
        ),
    ]


def test_resolve_automerge_auto_write():
    import warnings
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        assert _resolve_automerge(auto_write=True, automerge="none") == "none"
    assert _resolve_automerge(auto_write=False, automerge="pii") == "pii"
    assert _resolve_automerge(auto_write=False, automerge="both") == "both"


def test_code_scan_session_layout_and_autoflush(tmp_path, backend, store, sample_df):
    """scan() flushes artifacts under {session_id}/runs/{run_id}/artifacts/."""
    def _fake_scan(self, df, config, *, redibis_config=None):
        artifacts_dir = Path(config.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "pii_contract.yaml").write_text("apiVersion: v3.0.1\n", encoding="utf-8")
        return ScanResult(
            run_id=config.run_id,
            table=config.table,
            session_id=config.session_id,
            status="success",
            total_rows=len(df),
            total_columns=len(df.columns),
            artifacts={
                "pii_contract": str(artifacts_dir / "pii_contract.yaml"),
            },
        )

    output_root = tmp_path / "scan_output"
    session = CodeScanSession.create(
        sample_df,
        table="telecom.customers",
        output_root=output_root,
        backend=backend,
        store=store,
    )

    with patch("redibis.services.code_scan_session.ScanService.scan_dataframe", _fake_scan):
        result = session.scan(mode="pii", auto_write=False)

    assert result.session_id == session.session_id
    assert result.run_id.startswith("scan_")

    session_dir = output_root / session.session_id
    assert (session_dir / "data.csv").exists()
    assert (session_dir / "session.json").exists()

    run_dir = session_dir / "runs" / result.run_id
    manifest_path = run_dir / "run_manifest.json"
    artifacts_dir = run_dir / "artifacts"

    assert manifest_path.exists()
    assert artifacts_dir.exists()
    assert (artifacts_dir / "pii_contract.yaml").exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["session_id"] == session.session_id
    assert manifest["run_id"] == result.run_id
    assert manifest["artifacts"]["pii_contract"] == "pii_contract.yaml"

    index = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    assert index["latest_run_id"] == result.run_id
    assert index["runs"][0]["artifacts_dir"] == f"runs/{result.run_id}/artifacts"


def test_code_scan_session_open_roundtrip(tmp_path, backend, store, sample_df):
    output_root = tmp_path / "scan_output"
    created = CodeScanSession.create(
        sample_df, table="db.table", output_root=output_root,
        backend=backend, store=store,
    )
    reopened = CodeScanSession.open(
        created.session_id, output_root=output_root,
        backend=backend, store=store,
    )
    assert reopened.table == "db.table"
    assert reopened.session_id == created.session_id


@patch("redibis.services.pipeline.run_pii_detection")
def test_code_scan_pii_autoflush_artifacts(
    mock_pii, tmp_path, backend, store, sub_store, sample_df, mock_pii_detections,
):
    """PII-only scan writes subcontract + local artifacts (LocalBackend = MinIO stand-in)."""
    mock_pii.return_value = mock_pii_detections

    output_root = tmp_path / "scan_output"
    session = CodeScanSession.create(
        sample_df,
        table="telecom.customers",
        output_root=output_root,
        backend=backend,
        store=store,
        sub_store=sub_store,
    )
    result = session.scan(mode="pii", auto_write=False, generate_ge_docs=False)

    assert result.status == "success"
    artifacts_dir = session.session_dir / "runs" / result.run_id / "artifacts"
    assert (artifacts_dir / "pii_contract.yaml").exists()
    assert (artifacts_dir / "pii_detections.json").exists()

    assert backend.exists("pii-contracts", f"telecom.customers/{result.run_id}.yaml")
    assert store.get_active("telecom.customers") is None


@patch("redibis.services.pipeline.run_pii_detection")
def test_code_scan_auto_write_merges_active_contract(
    mock_pii, tmp_path, backend, store, sub_store, sample_df, mock_pii_detections,
):
    """automerge='both' merges run subcontracts into ContractStore (MinIO-compatible backend)."""
    mock_pii.return_value = mock_pii_detections

    session = CodeScanSession.create(
        sample_df,
        table="telecom.customers",
        output_root=tmp_path / "scan_output",
        backend=backend,
        store=store,
        sub_store=sub_store,
    )
    result = session.scan(mode="pii", automerge="both", generate_ge_docs=False)

    assert result.status == "success"
    assert result.pii_contract_version is not None

    active = store.get_active("telecom.customers")
    assert active is not None
    phone_col = next(p for p in active["schema"][0]["properties"] if p["name"] == "phone")
    assert col_pii_engine(phone_col).get("detected") is True

    sub = sub_store.get("pii", "telecom.customers", result.run_id)
    assert sub.status == "merged"
    assert sub.contract_uuid == active.get("contract_uuid")

    prefix = f"scan/telecom_customers/{session.session_id}/{result.run_id}/"
    assert backend.exists("pii-reports", f"{prefix}pii_contract.yaml")


def test_code_scan_quality_integration(tmp_path, backend, store, sub_store):
    """End-to-end quality scan writes GE artifacts into the session run layout."""
    pytest.importorskip("great_expectations")
    df = pd.DataFrame({
        "id": [1, 2, 3],
        "value": ["a", "b", "c"],
    })
    session = CodeScanSession.create(
        df,
        table="test.quality",
        output_root=tmp_path / "scan_output",
        backend=backend,
        store=store,
        sub_store=sub_store,
    )
    result = session.scan(mode="quality", auto_write=False, generate_ge_docs=False)
    assert result.status == "success"

    artifacts_dir = session.session_dir / "runs" / result.run_id / "artifacts"
    assert (artifacts_dir / "quality_contract.yaml").exists()
    assert (artifacts_dir / "interactive_review.html").exists()
    assert backend.exists("quality-contracts", f"test.quality/{result.run_id}.yaml")
