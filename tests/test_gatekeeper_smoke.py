"""
tests/test_gatekeeper_smoke.py
==============================
Extracted smoke test from the old gatekeeper.py inline __main__ block.

Tests the full quality + PII orchestration flow:
  QualityProfiler → QualityGatekeeper → PIIQualityBridge
  → Data Docs copy → ODCS contract export

Requires: great_expectations, numpy
Skip if GE not installed.
"""

import tempfile
from pathlib import Path

import pandas as pd
import pytest
import yaml

# Skip entire module if GE not installed
gx = pytest.importorskip("great_expectations", reason="Requires great_expectations")

from redibis.models import PIIDetection
from redibis.quality.profiler import QualityProfiler
from redibis.quality.gatekeeper import QualityGatekeeper


@pytest.fixture
def sample_dataframe():
    """Build a synthetic telecom DataFrame for testing."""
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(42)
    n = 1_000
    return pd.DataFrame({
        "customer_id":   rng.integers(1_000_000, 9_999_999, n).astype(str),
        "full_name":     ["Ahmed Mohamed"] * n,
        "phone":         ["+20100" + str(i).zfill(7) for i in range(n)],
        "email":         [f"user{i}@example.com" for i in range(n)],
        "national_id":   [f"290010114{str(i).zfill(5)}" for i in range(n)],
        "notes":         ["عميل VIP يحتاج متابعة"] * n,
        "city":          rng.choice(["القاهرة", "الإسكندرية", "الجيزة"], n).tolist(),
        "account_type":  rng.choice(["prepaid", "postpaid", "enterprise"], n).tolist(),
        "balance":       rng.uniform(0, 5000, n).round(2),
        "dt":            ["2026-05-09"] * n,
    })


@pytest.fixture
def simulated_detections():
    """Simulated PII detections (would come from PIIDetector in production)."""
    return [
        PIIDetection(
            column="phone", detected=True, entity_type="PHONE_NUMBER",
            confidence=0.92, equation_used="balanced",
            decision_path="presidio+gliner",
            presidio_score=0.88, presidio_pattern="msisdn_egypt_any_format",
            gliner_score=0.91, gliner_label="phone number",
            triage_score=0.54,
            regex_pattern=r"^(?:\+20|0020|0)?1[0125]\d{8}$",
            suggested_mostly=0.95, sample_match_rate=1.0,
        ),
        PIIDetection(
            column="full_name", detected=True, entity_type="PERSON",
            confidence=0.89, equation_used="balanced",
            decision_path="gliner+ge",
            presidio_score=0.40, gliner_score=0.89, gliner_label="person",
            triage_score=0.53, arabic_aware=False,
        ),
        PIIDetection(
            column="notes", detected=True, entity_type="PERSON",
            confidence=0.81, equation_used="balanced",
            decision_path="presidio+gliner+ar",
            presidio_score=0.75, gliner_score=0.80,
            arabic_aware=True, arabic_fraction=0.95, triage_score=0.61,
        ),
        PIIDetection(
            column="city", detected=False, equation_used="balanced",
            decision_path="skipped_by_triage", triage_score=0.11,
        ),
        PIIDetection(
            column="account_type", detected=False, equation_used="balanced",
            decision_path="skipped_by_triage", triage_score=0.12,
        ),
    ]


class TestQualityGatekeeperSmoke:
    """Full orchestration flow — quality checks in memory, then file-backed with docs."""

    def test_quality_only_in_memory(self, sample_dataframe):
        """Verify quality checks work in memory mode (no filesystem)."""
        qa = QualityGatekeeper(
            suite_name="test_quality_suite",
            in_memory=True,
        )
        qa.attach_dataframe(sample_dataframe, dataset_name="telecom_customers")
        qa.add_table_quality_checks(min_rows=100)
        qa.add_column_quality_checks("phone", not_null=True)
        qa.add_column_quality_checks("email", not_null=True)

        results = qa.run_tests(stage="quality_only", generate_docs=False)
        assert results.success is True

    def test_quality_contract_export_in_memory(self, sample_dataframe):
        """Verify quality contract export works without filesystem."""
        qa = QualityGatekeeper(
            suite_name="test_export_suite",
            in_memory=True,
        )
        qa.attach_dataframe(sample_dataframe, dataset_name="telecom_customers")
        qa.add_column_quality_checks("phone", not_null=True)
        qa.add_column_quality_checks("email", not_null=True, unique=True)

        contract = qa.export_quality_contract(
            database_name="telecom",
            table_name="customers",
        )

        assert contract["apiVersion"] == "v3.0.1"
        assert contract["database_name"] == "telecom"
        assert contract["table_name"] == "customers"
        assert contract["schema"][0]["physicalName"] == "telecom.customers"

        # Properties should contain quality rules
        props = {p["name"]: p for p in contract["schema"][0]["properties"]}
        assert "phone" in props
        assert "email" in props
        assert any(
            r.get("rule") in ("missingCount", "expect_column_values_to_not_be_null")
            for r in props["phone"]["quality"]
        )

    def test_file_backed_with_data_docs(self, sample_dataframe, simulated_detections):
        """Full file-backed flow: quality + PII bridge + Data Docs + ODCS export."""
        with tempfile.TemporaryDirectory() as run_dir:
            run_path = Path(run_dir)
            ge_proj  = run_path / "ge_project"

            qa = QualityGatekeeper(
                suite_name="smoke_pii_suite",
                in_memory=False,
                context_root_dir=ge_proj,
            )
            qa.attach_dataframe(sample_dataframe, dataset_name="telecom_customers")

            # Add quality checks
            qa.add_table_quality_checks(min_rows=100)
            qa.add_column_quality_checks("phone", not_null=True)

            # Register PII expectations directly (backward compat path)
            # In production, PIIQualityBridge would do this.
            for det in simulated_detections:
                if det.detected and det.regex_pattern:
                    qa.add_gx_expectation(
                        "expect_column_values_to_match_regex",
                        column=det.column,
                        regex=det.regex_pattern,
                        mostly=det.suggested_mostly,
                        meta={
                            "pii": {
                                "detected": det.detected,
                                "entity_type": det.entity_type,
                                "confidence": det.confidence,
                                "arabic_aware": det.arabic_aware,
                            },
                        },
                    )

            results = qa.run_tests(
                stage="post_pii_scan",
                generate_docs=True,
            )

            # Data Docs folder copy (Option B)
            ge_report_dir = run_path / "ge_report"
            index_html = qa.copy_data_docs_to(ge_report_dir)
            assert index_html.exists(), "GE report index.html not copied"
            assets = list(ge_report_dir.rglob("*.css")) + list(ge_report_dir.rglob("*.js"))
            assert len(assets) > 0, "No CSS/JS assets in copied GE report"

            # Export quality-only contract
            contract = qa.export_quality_contract(
                database_name="telecom",
                table_name="customers",
            )
            assert contract["apiVersion"] == "v3.0.1"
            assert len(contract["schema"][0]["properties"]) > 0

            # Legacy export with PII metadata pickup
            contract_path = run_path / "data_contract.yaml"
            qa.export_to_odcs(
                model_name="telecom_customers",
                output_path=str(contract_path),
            )
            assert contract_path.exists()

            with open(contract_path) as f:
                full_contract = yaml.safe_load(f)
            properties = full_contract["schema"][0]["properties"]
            col_map = {p["name"]: p for p in properties}

            # Phone should have the PII metadata from the meta block
            if "phone" in col_map and "pii" in col_map["phone"]:
                assert col_map["phone"]["pii"]["detected"] is True
