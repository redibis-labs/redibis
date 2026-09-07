"""End-to-end regression guard for contract creation refinement."""

from __future__ import annotations

import tempfile

import pandas as pd
import pytest

from redibis.pii.contract_writer import PIIContractWriter
from redibis.services.session_service import (
    ApprovedProperty,
    ApprovedSet,
    ScanSession,
    merge_approved,
    pii_row_to_fragment,
)
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

pytestmark = pytest.mark.integration


def _fixture_df() -> pd.DataFrame:
    return pd.DataFrame({
        "customer_id": ["C001", "C002", "C003", "C004", "C005"],
        "account_created_at": [
            "2024-01-15T10:30:00",
            "2024-02-20T14:00:00",
            "2024-03-01T09:15:00",
            "2024-04-10T16:45:00",
            "2024-05-05T11:00:00",
        ],
        "mobile_number": ["+201001234567", "01001234568", "01001234569", "+20111222333", "01099887766"],
        "national_id": ["29001011234567", "29505051234567", "3001011234567", "28512121234567", "30101011234567"],
        "loyalty_points": [10, 25, 0, 100, 5],
        "is_active": [1, 1, 0, 1, 1],
    })


def test_golden_contract_after_single_pii_approval(tmp_path):
    """Approve one PII column → full schema + policy-only privacy + telemetry sidecar."""
    df = _fixture_df()
    data_path = tmp_path / "customers.csv"
    df.to_csv(data_path, index=False)

    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="contracts")

    session = ScanSession(
        session_id="golden-001",
        table_name="eshop.customer_account",
        data_path=str(data_path),
    )
    session.total_columns = len(df.columns)

    # Build PII fragment from a detected phone column (same shape as scan approval).
    phone_row = {
        "column": "mobile_number",
        "detected": True,
        "entity_type": "PHONE_NUMBER",
        "confidence": 0.88,
        "presidio_score": 0.88,
        "presidio_pattern": "PHONE",
    }
    fragment = pii_row_to_fragment(phone_row)
    session.approved = ApprovedSet(items=[
        ApprovedProperty(
            prop_id="p1",
            kind="pii",
            column="mobile_number",
            payload=fragment,
        ),
    ])

    result = merge_approved(session, store, validate=False)
    assert result["merged_version"]

    active = store.get_active("eshop.customer_account")
    assert active is not None
    assert "provenance" not in active
    assert "pii_summary" not in active
    assert "_scan_metadata" not in active

    props = {p["name"]: p for p in active["schema"][0]["properties"]}
    assert set(props) == set(df.columns)

    for name, prop in props.items():
        assert prop.get("logicalType"), f"{name} missing logicalType"

    phone = props["mobile_number"]
    assert phone["logicalType"] == "string"
    assert phone.get("entity_type") == "PHONE_NUMBER"
    privacy = phone.get("privacy") or {}
    assert set(privacy.keys()) <= {"classification", "masking_policy"}
    assert "classification_engine" not in privacy
    assert "confidence" not in privacy
    assert "entity_type" not in privacy
    assert privacy.get("classification") == "pii_personal"
    assert privacy.get("masking_policy")

    ts = props["account_created_at"]
    assert ts["logicalType"] in ("timestamp", "date")

    nid = props["national_id"]
    assert nid["logicalType"] == "string"

    evidence = store.metadata.get_column_evidence("eshop.customer_account", "mobile_number")
    assert evidence.get("entity_type") == "PHONE_NUMBER"
    assert evidence.get("confidence") == pytest.approx(0.88)
    assert evidence.get("discovery_engines")


def test_schema_base_types_phone_and_timestamp():
    from redibis.contracts.schema_base import build_schema_base
    from redibis.contracts.type_inference import dtype_map_from_dataframe

    df = _fixture_df()
    partial = build_schema_base(
        "eshop.customer_account",
        dtype_map_from_dataframe(df),
        df=df,
    )
    props = {p["name"]: p for p in partial["schema"][0]["properties"]}
    assert props["mobile_number"]["logicalType"] == "string"
    assert props["national_id"]["logicalType"] == "string"
    assert props["account_created_at"]["logicalType"] in ("timestamp", "date")
    assert props["loyalty_points"]["logicalType"] == "integer"


def test_pii_writer_policy_only_privacy():
    from redibis.models import PIIDetection

    writer = PIIContractWriter("eshop", "customer_account", run_id="r1")
    writer.add_detection(PIIDetection(
        column="mobile_number",
        detected=True,
        entity_type="PHONE_NUMBER",
        confidence=0.88,
        presidio_score=0.88,
    ))
    partial = writer.build()
    prop = partial["schema"][0]["properties"][0]
    privacy = prop["privacy"]
    assert "classification" in privacy
    assert "masking_policy" in privacy
    assert "classification_engine" not in privacy
    assert prop["entity_type"] == "PHONE_NUMBER"
    assert "_scan_metadata" in partial
    telemetry = partial["_column_telemetry"]["mobile_number"]
    assert telemetry["confidence"] == 0.88

    from redibis.store.contract_metadata import slim_contract

    slim = slim_contract(partial)
    assert "_column_telemetry" not in slim
    assert "_scan_metadata" not in slim
