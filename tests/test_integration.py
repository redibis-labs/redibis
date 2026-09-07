from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest
import yaml

from redibis.pii.equations import decide_pii
from redibis.pii.thresholds import Thresholds
from redibis.models import PIIDetection, ColumnProfile
from redibis.pii.detector import detect_pii as detect_pii
from redibis.pii.llm_refiner import refine_detections as run_layer4_refinement
from tests.contract_helpers import col_pii_engine
from redibis.pii.run_outputs import write_pii_run_outputs as write_pii_outputs
from redibis.quality.run_outputs import write_ge_run_outputs as write_ge_outputs
from redibis.pii.contract_writer import PIIContractWriter
from redibis.store.storage_backend import LocalBackend, RunOutputWriter
from redibis.store.contract_store import ContractStore
from redibis.store.merger import merge_two_contracts


@pytest.fixture
def synthetic_df():
    return pd.DataFrame({
        "phone": ["+201012345678", "+201112345678", "+201234567890"] * 1700,
        "national_id": ["29901011234567", "29901021234568", "29901031234569"] * 1700,
        "email": ["user1@telecom.eg", "user2@telecom.eg", "user3@telecom.eg"] * 1700,
        "name": ["Ahmed Mohamed", "Fatma Ali", "Omar Hassan"] * 1700,
        "transaction_id": ["TXN001", "TXN002", "TXN003"] * 1700,
        "amount": [100.0, 200.0, 300.0] * 1700,
    })


@pytest.fixture
def triage_signals():
    return [
        ColumnProfile(
            column="phone", dtype="object", cardinality_ratio=0.5,
            avg_value_length=14.0, null_rate=0.0, name_hint_score=1.0,
            arabic_fraction=0.0, triage_score=0.8, send_to_detector=True,
            sample_values=["+201012345678"],
        ),
        ColumnProfile(
            column="national_id", dtype="object", cardinality_ratio=0.5,
            avg_value_length=14.0, null_rate=0.0, name_hint_score=1.0,
            arabic_fraction=0.0, triage_score=0.8, send_to_detector=True,
            sample_values=["29901011234567"],
        ),
        ColumnProfile(
            column="email", dtype="object", cardinality_ratio=0.5,
            avg_value_length=18.0, null_rate=0.0, name_hint_score=1.0,
            arabic_fraction=0.0, triage_score=0.8, send_to_detector=True,
            sample_values=["user1@telecom.eg"],
        ),
        ColumnProfile(
            column="name", dtype="object", cardinality_ratio=0.5,
            avg_value_length=12.0, null_rate=0.0, name_hint_score=1.0,
            arabic_fraction=0.0, triage_score=0.8, send_to_detector=True,
            sample_values=["Ahmed Mohamed"],
        ),
        ColumnProfile(
            column="transaction_id", dtype="object", cardinality_ratio=0.5,
            avg_value_length=6.0, null_rate=0.0, name_hint_score=0.0,
            arabic_fraction=0.0, triage_score=0.1, send_to_detector=False,
            sample_values=["TXN001"],
        ),
        ColumnProfile(
            column="amount", dtype="float64", cardinality_ratio=0.5,
            avg_value_length=5.0, null_rate=0.0, name_hint_score=0.0,
            arabic_fraction=0.0, triage_score=0.05, send_to_detector=False,
            sample_values=["100.0"],
        ),
    ]


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield LocalBackend(tmpdir)


@pytest.fixture
def store(backend):
    return ContractStore(backend, bucket="pii-contracts")


class TestEquationEndToEnd:
    def test_balanced_equation_on_mixed_detections(self):
        raw = [
            PIIDetection(column="phone", detected=False, presidio_score=0.85, gliner_score=0.75),
            PIIDetection(column="national_id", detected=False, presidio_score=0.90, gliner_score=0.40),
            PIIDetection(column="email", detected=False, presidio_score=0.55, gliner_score=0.60),
            PIIDetection(column="name", detected=False, presidio_score=0.30, gliner_score=0.80),
            PIIDetection(column="transaction_id", detected=False),
        ]
        thresholds = Thresholds()
        results = [decide_pii(d, "balanced", thresholds) for d in raw]

        # phone: presidio=0.85>=0.60=yes, gliner=0.75>=0.50=yes → 2 yes → detected
        # national_id: presidio=0.90>=0.60=yes, gliner=0.40<0.50=no → 1 yes, presidio>=0.9 very_high → detected
        # email: presidio=0.55<0.60=no, gliner=0.60>=0.50=yes → 1 yes, no very_high → NOT detected
        # name: presidio=0.30<0.60=no, gliner=0.80>=0.50=yes → 1 yes, no very_high → NOT detected
        # transaction_id: no engines → NOT detected
        assert results[0].detected is True
        assert results[1].detected is True
        assert results[2].detected is False
        assert results[3].detected is False
        assert results[4].detected is False

        assert results[0].confidence > 0
        assert results[0].equation_used == "balanced"


class TestPIIContractWriterEndToEnd:
    def test_writer_produces_valid_contract(self):
        detections = [
            PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER",
                         confidence=0.85, equation_used="balanced", presidio_score=0.85,
                         gliner_score=0.75),
            PIIDetection(column="national_id", detected=True, entity_type="NATIONAL_ID",
                         confidence=0.90, equation_used="balanced", presidio_score=0.90),
            PIIDetection(column="transaction_id", detected=False, equation_used="balanced"),
        ]
        writer = PIIContractWriter("telecom", "customers", equation_used="balanced", run_id="test-r1")
        for d in detections:
            writer.add_detection(d)
        contract = writer.build()

        assert contract["database_name"] == "telecom"
        assert contract["table_name"] == "customers"
        props = contract["schema"][0]["properties"]
        phone = next(p for p in props if p["name"] == "phone")
        phone_pii = col_pii_engine(phone)
        assert phone_pii["detected"] is True
        assert phone_pii["entity_type"] == "PHONE_NUMBER"

        txn = next(p for p in props if p["name"] == "transaction_id")
        assert col_pii_engine(txn).get("detected") is not True


class TestLayer3WithMockedEngines:
    def test_layer3_produces_detections_with_skipped_columns(self, synthetic_df, triage_signals):
        from redibis.pii.ner_backend import NERHit, NERReport

        fake_backend = MagicMock()
        fake_backend.name = "fake:test"
        fake_backend.analyze.return_value = NERReport(
            model="fake:test",
            labels_requested=["phone number"],
            hits=[NERHit(label="phone number", score=0.75, match_rate=0.8)],
        )
        with patch("redibis.pii.detector._run_presidio") as mock_presidio:
            mock_presidio.return_value = {
                "score": 0.85, "pattern": "msisdn_egypt", "match_rate": 0.7,
                "entity_type": "PHONE_NUMBER", "regex_hits": [],
            }
            detections = detect_pii(
                synthetic_df,
                columns=triage_signals,
                gliner_always_run=True,
                ner_backend=fake_backend,
            )

        assert len(detections) == 6

        scanned = [d for d in detections if d.decision_path != "skipped_by_triage"]
        skipped = [d for d in detections if d.decision_path == "skipped_by_triage"]
        assert len(scanned) == 4
        assert len(skipped) == 2

        phone_det = next(d for d in detections if d.column == "phone")
        assert phone_det.presidio_score == 0.85
        assert phone_det.gliner_score == 0.75
        assert phone_det.detected is False  # Layer 3 doesn't apply equation
        assert phone_det.entity_type == "PHONE_NUMBER"

        txn_det = next(d for d in detections if d.column == "transaction_id")
        assert txn_det.detected is False
        assert txn_det.decision_path == "skipped_by_triage"


class TestLayer5ReportsOutput:
    def test_pii_outputs_written_to_store(self, synthetic_df, backend, store):
        detections = [
            PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER",
                         confidence=0.85, equation_used="balanced", presidio_score=0.85,
                         gliner_score=0.75, presidio_match_rate=0.7, gliner_match_rate=0.8),
            PIIDetection(column="transaction_id", detected=False, equation_used="balanced"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            from redibis.models import RunMetadata
            run_metadata = RunMetadata(
                run_id="test-r1", scan_date="2026-05-10T00:00:00Z",
                equation_used="balanced", table_physical_name="telecom.customers",
            )
            result = write_pii_outputs(
                synthetic_df, detections, run_metadata,
                table="telecom.customers", run_id="test-r1",
                equation_used="balanced", output_dir=Path(tmpdir),
                backend=backend, store=store, runs_bucket="pii-reports",
            )

        assert result["detected_count"] == 1
        assert result["total_scanned"] == 2
        assert result["upsert_result"].is_new is True

        active = store.get_active("telecom.customers")
        assert active is not None
        props = active["schema"][0]["properties"]
        phone = next(p for p in props if p["name"] == "phone")
        assert col_pii_engine(phone)["detected"] is True


class TestFullPIIWorkflowIntegration:
    def test_three_workflows_merge_correctly(self, backend, store, synthetic_df):
        table = "telecom.customers"

        # Workflow A — GE partial
        ge_partial = {
            "schema": [{
                "name": "telecom_customers",
                "physicalName": "telecom.customers",
                "properties": [
                    {"name": "phone", "logicalType": "string",
                     "physicalType": "VARCHAR(20)",
                     "quality": [{"rule": "not_null"}]},
                    {"name": "national_id", "logicalType": "string",
                     "quality": [{"rule": "length_eq_14"}]},
                ],
            }],
        }
        store.upsert(ge_partial, table=table, workflow="ge", run_id="ge-r1")

        # Workflow B — PII partial
        detections = [
            PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER",
                         confidence=0.85, equation_used="balanced", presidio_score=0.85,
                         gliner_score=0.75),
            PIIDetection(column="national_id", detected=True, entity_type="NATIONAL_ID",
                         confidence=0.90, equation_used="balanced", presidio_score=0.90),
        ]
        writer = PIIContractWriter("telecom", "customers", equation_used="balanced", run_id="pii-r1")
        for d in detections:
            writer.add_detection(d)
        pii_partial = writer.build()
        store.upsert(pii_partial, table=table, workflow="pii", run_id="pii-r1")

        # Workflow C — Business partial
        biz_partial = {
            "schema": [{
                "name": "telecom_customers",
                "properties": [{
                    "name": "phone",
                    "businessName": "Mobile Phone",
                    "description": "E.164 phone number",
                    "tags": ["regulated"],
                }],
            }],
        }
        store.upsert(biz_partial, table=table, workflow="business", run_id="biz-r1")

        active = store.get_active(table)
        phone = next(p for p in active["schema"][0]["properties"] if p["name"] == "phone")

        assert phone["physicalType"] == "VARCHAR(20)"
        assert col_pii_engine(phone)["detected"] is True
        assert phone["businessName"] == "Mobile Phone"
        assert phone["classification"] == "pii_personal"
        assert "pii" in phone["tags"]
        assert "regulated" in phone["tags"]

        history = store.get_history(table)
        assert len(history) == 3

    def test_pii_rerun_replaces_only_pii_block(self, backend, store):
        table = "telecom.customers"

        # GE first
        store.upsert(
            {"schema": [{"name": "t", "physicalName": table,
                         "properties": [{"name": "phone", "quality": [{"rule": "x"}]}]}]},
            table=table, workflow="ge", run_id="ge-r1",
        )

        # PII run 1
        w1 = PIIContractWriter("telecom", "customers", run_id="pii-r1")
        w1.add_detection(PIIDetection(column="phone", detected=True,
                                       entity_type="PHONE_NUMBER", confidence=0.85))
        store.upsert(w1.build(), table=table, workflow="pii", run_id="pii-r1")

        # PII run 2 (higher confidence)
        w2 = PIIContractWriter("telecom", "customers", run_id="pii-r2")
        w2.add_detection(PIIDetection(column="phone", detected=True,
                                       entity_type="PHONE_NUMBER", confidence=0.95))
        store.upsert(w2.build(), table=table, workflow="pii", run_id="pii-r2")

        active = store.get_active(table)
        phone = active["schema"][0]["properties"][0]
        evidence = store.metadata.get_column_evidence(table, "phone")
        assert evidence.get("confidence") == 0.95
        assert "confidence" not in (phone.get("privacy") or {})
        assert phone["quality"]  # GE quality preserved
