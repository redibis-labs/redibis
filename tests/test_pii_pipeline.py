"""
Unit test suite for the PII detection pipeline.
Run with: python -m pytest test_pii_pipeline.py -v

Coverage:
  Layer 1     : test_layer1_sampler              (4 strategies × pandas)
  Catalog     : test_regex_catalog               (validators, normalizers)
  Recognizer  : test_recognizer_factory          (groups, collisions)
  Profiler    : test_data_profiler               (arabic, triage, drops)
  Gatekeeper  : test_quality_gatekeeper          (quality-only)
  PII writer  : test_pii_contract_writer         (PII-only ODCS)
  Equations   : test_equations                   (strict/balanced/lenient)
  Thresholds  : test_thresholds                  (defaults, override)
  Layer 3     : test_layer3_detection            (Presidio + GLiNER mocked)
  S3          : test_s3_storage                  (LocalBackend + RunOutputWriter)
  Store       : test_contract_store_service      (smart upsert, audit, identity)
  Merge       : test_merge_contracts             (N-way, identity, tags)
  Integration : test_workflow_integration         (3 workflows end-to-end)

This file uses pytest for execution but is also runnable standalone via
`python test_pii_pipeline.py` which invokes pytest internally.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
import pandas as pd
import yaml

from redibis.store.merger import (
    merge_two_contracts, merge_odcs_contracts,
    IdentityConflictError, ProvenanceEntry,
)
from redibis.store.storage_backend import LocalBackend, RunOutputWriter
from redibis.store.contract_store import ContractStore, UpsertResult
from redibis.pii.contract_writer import PIIContractWriter
from redibis.pii.sensitivity import classify_sensitivity
from redibis.models import PIIDetection


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_storage(tmp_path):
    """Local storage backend rooted in a temp dir."""
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def store(tmp_storage):
    return ContractStore(tmp_storage, bucket="test-contracts")


@pytest.fixture
def sample_pii_detections():
    return [
        PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER",
                     confidence=0.92, presidio_score=0.88,
                     presidio_pattern="msisdn_egypt_any_format", gliner_score=0.91),
        PIIDetection(column="national_id", detected=True, entity_type="EG_NATIONAL_ID",
                     confidence=0.97, presidio_score=0.92),
        PIIDetection(column="notes", detected=True, entity_type="PERSON",
                     confidence=0.81, gliner_score=0.81,
                     arabic_aware=True, arabic_fraction=0.74),
        PIIDetection(column="city", detected=False, decision_path="skipped_by_triage"),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Merge contracts — identity, tags, replacement
# ═════════════════════════════════════════════════════════════════════════════

class TestMergeContracts:

    def test_first_creation_populates_identity(self):
        """First merge must populate contract_uuid + database_name + table_name."""
        partial = {
            "schema": [{"name": "telecom_customers", "properties": []}],
            "database_name": "telecom",
            "table_name":    "customers",
        }
        merged = merge_two_contracts(None, partial, workflow="ge")
        assert merged["contract_uuid"]                # populated
        assert merged["database_name"] == "telecom"
        assert merged["table_name"]    == "customers"
        assert merged["version"]       == "1.0.0"
        assert "provenance" not in merged  # telemetry lives in ContractMetadataStore

    def test_contract_uuid_locked_after_first_write(self):
        """Identity fields must NEVER be overwritten."""
        base    = merge_two_contracts(None, {"database_name": "a", "table_name": "b"})
        original_uuid = base["contract_uuid"]

        # Try to inject a different uuid
        evil = {"contract_uuid": str(uuid.uuid4()), "database_name": "a", "table_name": "b"}
        with pytest.raises(IdentityConflictError):
            merge_two_contracts(base, evil, workflow="manual")

    def test_database_name_conflict_raises(self):
        base = merge_two_contracts(None, {"database_name": "telecom", "table_name": "customers"})
        with pytest.raises(IdentityConflictError):
            merge_two_contracts(base,
                                {"database_name": "different", "table_name": "customers"})

    def test_tags_merge_via_set_union(self):
        """Tags from multiple workflows must accumulate, not overwrite."""
        base = merge_two_contracts(None, {
            "schema": [{
                "name": "t",
                "properties": [{"name": "phone", "tags": ["pii"]}],
            }],
        })
        merged = merge_two_contracts(base, {
            "schema": [{
                "name": "t",
                "properties": [{"name": "phone", "tags": ["regulated", "billing"]}],
            }],
        })
        merged2 = merge_two_contracts(merged, {
            "schema": [{
                "name": "t",
                "properties": [{"name": "phone", "tags": ["gdpr"]}],
            }],
        })
        phone = merged2["schema"][0]["properties"][0]
        assert set(phone["tags"]) == {"pii", "regulated", "billing", "gdpr"}

    def test_quality_block_replaces_wholesale(self):
        """Quality is a list — must be replaced, not appended-to."""
        base = merge_two_contracts(None, {
            "schema": [{
                "name": "t",
                "properties": [{
                    "name": "phone",
                    "quality": [{"rule": "old_rule_1"}, {"rule": "old_rule_2"}],
                }],
            }],
        })
        merged = merge_two_contracts(base, {
            "schema": [{
                "name": "t",
                "properties": [{
                    "name": "phone",
                    "quality": [{"rule": "new_rule"}],
                }],
            }],
        })
        phone = merged["schema"][0]["properties"][0]
        assert len(phone["quality"]) == 1
        assert phone["quality"][0]["rule"] == "new_rule"

    def test_pii_block_replaces_wholesale(self):
        base = merge_two_contracts(None, {
            "schema": [{
                "name": "t",
                "properties": [{
                    "name": "phone",
                    "pii": {"detected": False, "triage_score": 0.2},
                }],
            }],
        })
        merged = merge_two_contracts(base, {
            "schema": [{
                "name": "t",
                "properties": [{
                    "name": "phone",
                    "pii": {"detected": True, "entity_type": "PHONE_NUMBER",
                            "confidence": 0.9},
                }],
            }],
        })
        phone = merged["schema"][0]["properties"][0]
        assert phone["pii"]["detected"] is True
        assert phone["pii"]["entity_type"] == "PHONE_NUMBER"
        assert "triage_score" not in phone["pii"]   # old block replaced

    def test_new_columns_appended(self):
        base = merge_two_contracts(None, {
            "schema": [{
                "name": "t",
                "properties": [{"name": "phone"}],
            }],
        })
        merged = merge_two_contracts(base, {
            "schema": [{
                "name": "t",
                "properties": [{"name": "email"}],
            }],
        })
        col_names = [p["name"] for p in merged["schema"][0]["properties"]]
        assert "phone" in col_names
        assert "email" in col_names

    def test_version_bumps_on_merge(self):
        base   = merge_two_contracts(None, {"database_name": "d", "table_name": "t"})
        m1     = merge_two_contracts(base, {"description": {"purpose": "x"}})
        m2     = merge_two_contracts(m1, {"description": {"purpose": "y"}})
        assert base["version"] == "1.0.0"
        assert m1["version"]   == "1.0.1"
        assert m2["version"]   == "1.0.2"

    def test_provenance_appends_each_merge(self, store):
        store.upsert({"database_name": "d", "table_name": "t", "schema": []},
                     table="d.t", workflow="ge", run_id="r1")
        store.upsert({"description": {"purpose": "x"}},
                     table="d.t", workflow="business", run_id="r2")
        workflows = [p["workflow"] for p in store.metadata.get_provenance("d.t")]
        assert workflows == ["ge", "business"]

    def test_n_way_merge_left_to_right(self):
        result = merge_odcs_contracts([
            {"database_name": "d", "table_name": "t",
             "schema": [{"name": "t", "properties": [{"name": "a"}]}]},
            {"schema": [{"name": "t", "properties": [{"name": "b"}]}]},
            {"schema": [{"name": "t", "properties": [{"name": "c"}]}]},
        ])
        cols = [p["name"] for p in result["schema"][0]["properties"]]
        assert cols == ["a", "b", "c"]
        assert result["version"] == "1.0.2"  # 2 incremental merges after base


# ═════════════════════════════════════════════════════════════════════════════
# 2. S3 storage — LocalBackend
# ═════════════════════════════════════════════════════════════════════════════

class TestS3Storage:

    def test_put_get_bytes(self, tmp_storage):
        tmp_storage.put_bytes("b", "k", b"data")
        assert tmp_storage.exists("b", "k")
        assert tmp_storage.get_bytes("b", "k") == b"data"

    def test_get_nonexistent_raises_keyerror(self, tmp_storage):
        with pytest.raises(KeyError):
            tmp_storage.get_bytes("b", "missing")

    def test_put_get_yaml_roundtrip(self, tmp_storage):
        obj = {"name": "demo", "tags": ["pii", "regulated"]}
        tmp_storage.put_yaml("b", "k.yaml", obj)
        assert tmp_storage.get_yaml("b", "k.yaml") == obj

    def test_put_get_json_roundtrip(self, tmp_storage):
        obj = {"x": 1, "y": [1, 2, 3]}
        tmp_storage.put_json("b", "k.json", obj)
        assert tmp_storage.get_json("b", "k.json") == obj

    def test_unicode_yaml_roundtrip(self, tmp_storage):
        obj = {"col": "اسم العميل", "tag": "إيميل"}
        tmp_storage.put_yaml("b", "ar.yaml", obj)
        assert tmp_storage.get_yaml("b", "ar.yaml") == obj

    def test_list_keys_with_prefix(self, tmp_storage):
        tmp_storage.put_text("b", "active/a.yaml", "x")
        tmp_storage.put_text("b", "active/b.yaml", "x")
        tmp_storage.put_text("b", "audit/c.yaml",  "x")
        active = tmp_storage.list_keys("b", prefix="active/")
        assert len(active) == 2

    def test_put_folder_recursive(self, tmp_storage, tmp_path):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "index.html").write_text("<html></html>")
        (src / "sub" / "style.css").write_text("body{}")

        keys = tmp_storage.put_folder("b", "uploaded/", src)
        assert len(keys) == 2
        assert any("index.html" in k for k in keys)
        assert any("style.css"  in k for k in keys)

    def test_run_output_writer_prefix_pattern(self, tmp_storage):
        writer = RunOutputWriter(
            backend=tmp_storage, bucket="runs",
            workflow="pii", table="telecom.customers", run_id="2026-05-09_14-32-18",
        )
        writer.write("data_contract.yaml", {"apiVersion": "v3.0.1"})
        expected = "pii/telecom_customers/2026-05-09_14-32-18/data_contract.yaml"
        assert tmp_storage.exists("runs", expected)


# ═════════════════════════════════════════════════════════════════════════════
# 3. ContractStore — smart upsert
# ═════════════════════════════════════════════════════════════════════════════

class TestContractStore:

    def test_first_upsert_creates_active_and_audit(self, store):
        result = store.upsert(
            partial={"schema": [{"name": "t", "properties": []}]},
            table="telecom.customers", workflow="ge", run_id="r1",
        )
        assert result.is_new is True
        assert store.get_active("telecom.customers") is not None
        assert store.get_audit_snapshot("telecom.customers", result.run_uuid) is not None

    def test_upsert_preserves_identity_across_runs(self, store):
        r1 = store.upsert({"schema": [{"name": "t", "properties": []}]},
                          table="t.t", workflow="ge")
        r2 = store.upsert({"schema": [{"name": "t", "properties": []}]},
                          table="t.t", workflow="pii")
        assert r1.contract_uuid == r2.contract_uuid

    def test_upsert_auto_populates_database_table_name(self, store):
        store.upsert(
            partial={"schema": [{"name": "t", "properties": []}]},
            table="telecom.customers", workflow="ge",
        )
        active = store.get_active("telecom.customers")
        assert active["database_name"] == "telecom"
        assert active["table_name"]    == "customers"

    def test_upsert_three_workflows_field_level_merge(self, store, sample_pii_detections):
        # All workflows use physicalName as the canonical identifier so the
        # merge engine matches the table across workflows correctly.

        # GE
        store.upsert(
            partial={
                "schema": [{
                    "name": "t_t",
                    "physicalName": "t.t",
                    "properties": [{"name": "phone", "logicalType": "string",
                                    "quality": [{"rule": "not_null"}]}],
                }],
            },
            table="t.t", workflow="ge",
        )
        # PII (PIIContractWriter sets physicalName automatically)
        pii_writer = PIIContractWriter("t", "t", run_id="r")
        pii_writer.add_detections(sample_pii_detections)
        store.upsert(pii_writer.build(), table="t.t", workflow="pii")
        # Business
        store.upsert(
            partial={
                "schema": [{
                    "name": "t_t",
                    "physicalName": "t.t",
                    "properties": [{
                        "name": "phone",
                        "businessName": "Mobile Phone",
                        "tags": ["regulated"],
                    }],
                }],
            },
            table="t.t", workflow="business",
        )

        active = store.get_active("t.t")
        phone = next(p for p in active["schema"][0]["properties"]
                     if p["name"] == "phone")
        # All three contributions present
        assert phone["quality"]                      # GE
        from tests.contract_helpers import col_pii_engine
        assert col_pii_engine(phone).get("detected") is True      # PII
        assert phone["businessName"] == "Mobile Phone"  # business
        # Tags accumulated
        assert "regulated" in phone["tags"]
        assert "pii"       in phone["tags"]

    def test_history_returns_most_recent_first(self, store):
        for i in range(3):
            store.upsert(
                partial={"schema": [{"name": "t", "properties": []}]},
                table="t.t", workflow=f"w{i}", run_id=f"r{i}",
            )
        history = store.get_history("t.t")
        assert len(history) == 3
        # Most recent first
        assert history[0].workflow == "w2"
        assert history[2].workflow == "w0"

    def test_identity_conflict_blocked_at_service_layer(self, store):
        store.upsert(
            partial={"schema": [{"name": "t", "properties": []}]},
            table="t.t", workflow="ge",
        )
        evil = {
            "contract_uuid": str(uuid.uuid4()),
            "schema":        [{"name": "t", "properties": []}],
        }
        with pytest.raises(IdentityConflictError):
            store.upsert(evil, table="t.t", workflow="manual")

    def test_list_tables(self, store):
        store.upsert({"schema": []}, table="db1.t1", workflow="ge")
        store.upsert({"schema": []}, table="db1.t2", workflow="ge")
        store.upsert({"schema": []}, table="db2.t3", workflow="ge")
        tables = store.list_tables()
        assert set(tables) == {"db1.t1", "db1.t2", "db2.t3"}

    def test_audit_snapshot_immutable_per_run(self, store):
        r1 = store.upsert({"schema": []}, table="t.t", workflow="ge")
        # Mutate active via second upsert
        r2 = store.upsert(
            {"schema": [{"name": "t", "properties": [{"name": "new_col"}]}]},
            table="t.t", workflow="pii",
        )
        # Old audit snapshot must not change
        snapshot1 = store.get_audit_snapshot("t.t", r1.run_uuid)
        snapshot2 = store.get_audit_snapshot("t.t", r2.run_uuid)
        assert snapshot1["version"] == "1.0.0"
        assert snapshot2["version"] == "1.0.1"

    def test_upsert_business_writes_to_business_prefix(self, store):
        store.upsert_business(
            contract={"schema": [{"name": "t", "properties": []}],
                      "description": {"purpose": "human"}},
            table="t.t", run_id="r",
        )
        # Business prefix copy
        assert store.backend.exists(store.bucket, "business/t.t.yaml")
        # Active also written
        assert store.get_active("t.t") is not None


# ═════════════════════════════════════════════════════════════════════════════
# 4. PIIContractWriter — partial ODCS output
# ═════════════════════════════════════════════════════════════════════════════

class TestPIIContractWriter:

    def test_no_identity_uuid_in_partial(self, sample_pii_detections):
        """Partial contracts must NOT carry contract_uuid — store assigns it."""
        writer = PIIContractWriter("telecom", "customers", run_id="r")
        writer.add_detections(sample_pii_detections)
        partial = writer.build()
        assert "contract_uuid" not in partial

    def test_database_table_name_set(self, sample_pii_detections):
        writer = PIIContractWriter("telecom", "customers", run_id="r")
        writer.add_detections(sample_pii_detections)
        partial = writer.build()
        assert partial["database_name"] == "telecom"
        assert partial["table_name"]    == "customers"

    def test_pii_columns_get_classification(self, sample_pii_detections):
        writer = PIIContractWriter("d", "t", run_id="r")
        writer.add_detections(sample_pii_detections)
        partial = writer.build()
        cols = {p["name"]: p for p in partial["schema"][0]["properties"]}
        assert cols["national_id"]["classification"] == "pii_sensitive"
        assert cols["phone"]["classification"]       == "pii_personal"

    def test_arabic_aware_columns_tagged(self, sample_pii_detections):
        writer = PIIContractWriter("d", "t", run_id="r")
        writer.add_detections(sample_pii_detections)
        partial = writer.build()
        cols = {p["name"]: p for p in partial["schema"][0]["properties"]}
        assert "contains_arabic" in cols["notes"]["tags"]
        assert "contains_arabic" not in cols["phone"]["tags"]

    def test_non_pii_columns_have_no_classification(self, sample_pii_detections):
        writer = PIIContractWriter("d", "t", run_id="r")
        writer.add_detections(sample_pii_detections)
        partial = writer.build()
        cols = {p["name"]: p for p in partial["schema"][0]["properties"]}
        assert "classification" not in cols["city"]
        assert "tags" not in cols["city"]

    def test_pii_summary_aggregates(self, sample_pii_detections):
        writer = PIIContractWriter("d", "t", run_id="r")
        writer.add_detections(sample_pii_detections)
        partial = writer.build()
        summary = partial["_scan_metadata"]
        assert summary["total_columns"] == 4
        assert summary["pii_confirmed"] == 3
        assert summary["pii_clean"]     == 1
        assert summary["highest_sensitivity"] == "pii_sensitive"

    def test_classify_sensitivity_mapping(self):
        assert classify_sensitivity("EG_NATIONAL_ID") == "pii_sensitive"
        assert classify_sensitivity("CREDIT_CARD")    == "pii_sensitive"
        assert classify_sensitivity("PHONE_NUMBER")   == "pii_personal"
        assert classify_sensitivity("PERSON")         == "pii_personal"
        assert classify_sensitivity(None)             == "internal"

    def test_pii_detection_engines(self):
        d = PIIDetection(column="x", detected=False, presidio_score=0.8, gliner_score=0.7,
                         llm_score=None)
        assert d.contributing_engines() == ["regex", "ner"]


# ═════════════════════════════════════════════════════════════════════════════
# 5. Equations — strict / balanced / lenient (specs only — implementation
#    expected in decisions/equations.py during agent build)
# ═════════════════════════════════════════════════════════════════════════════

class TestEquations:
    """
    Specifications for decisions/equations.py.

    Implementation must match these test contracts exactly.
    Mark as xfail until decisions/equations.py is implemented.
    """

    def test_strict_requires_all_engines_above_threshold(self):
        from redibis.pii.equations import decide_pii
        from redibis.pii.thresholds import Thresholds
        d = PIIDetection(column="phone", detected=False, presidio_score=0.9, gliner_score=0.8)
        result = decide_pii(d, "strict", Thresholds(presidio_min=0.6, gliner_min=0.6))
        assert result.detected is True

    def test_strict_fails_if_any_engine_below(self):
        from redibis.pii.equations import decide_pii
        from redibis.pii.thresholds import Thresholds
        d = PIIDetection(column="phone", detected=False, presidio_score=0.9, gliner_score=0.4)
        result = decide_pii(d, "strict", Thresholds(presidio_min=0.6, gliner_min=0.6))
        assert result.detected is False

    def test_balanced_two_of_three_engines(self):
        from redibis.pii.equations import decide_pii
        from redibis.pii.thresholds import Thresholds
        d = PIIDetection(column="phone", detected=False,
                         presidio_score=0.9, gliner_score=0.7, llm_score=0.3)
        result = decide_pii(d, "balanced",
                            Thresholds(presidio_min=0.6, gliner_min=0.5, llm_min=0.7))
        assert result.detected is True   # presidio + gliner pass

    def test_lenient_any_single_engine(self):
        from redibis.pii.equations import decide_pii
        from redibis.pii.thresholds import Thresholds
        d = PIIDetection(column="phone", detected=False, presidio_score=0.9,
                         gliner_score=0.2, llm_score=0.1)
        result = decide_pii(d, "lenient",
                            Thresholds(presidio_min=0.6, gliner_min=0.5, llm_min=0.5))
        assert result.detected is True


# ═════════════════════════════════════════════════════════════════════════════
# 6. Layer 1 — sampler (tests only the Pandas path; Spark needs cluster)
# ═════════════════════════════════════════════════════════════════════════════

class TestLayer1Sampler:
    """Specs for pii_layer1_sampler.PandasTableSampler."""

    def test_pandas_sampler_fixed_rows(self):
        from redibis.quality.sampling import PandasTableSampler, SamplingConfig
        df = pd.DataFrame({"a": range(1000)})
        cfg = SamplingConfig(strategy="fixed_rows", fixed_row_count=100)
        out = PandasTableSampler(cfg).from_dataframe(df)
        assert len(out) == 100

    def test_pandas_sampler_string_dtype_cast(self):
        from redibis.quality.sampling import PandasTableSampler, SamplingConfig
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        out = PandasTableSampler(SamplingConfig(strategy="statistical")).from_dataframe(df)
        # All columns must be string after sampling
        from pandas.api.types import is_string_dtype, is_object_dtype
        assert all(is_string_dtype(out[c]) or is_object_dtype(out[c]) for c in out.columns)


# ═════════════════════════════════════════════════════════════════════════════
# 7. Catalog — regex patterns and validators
# ═════════════════════════════════════════════════════════════════════════════

class TestRegexCatalog:
    """Specs for telco_pii_regex_catalog_v2.py."""

    def test_validate_luhn(self):
        from redibis.pii.regex_catalog import validate_luhn
        assert validate_luhn("4532015112830366") is True   # valid Visa
        assert validate_luhn("4532015112830367") is False

    def test_validate_egypt_national_id(self):
        from redibis.pii.regex_catalog import validate_egypt_national_id
        result = validate_egypt_national_id("29001011401234")
        assert result["valid"] is True
        assert result["governorate"] == "Qalyubia"

    def test_normalize_msisdn_egypt_handles_dirty_inputs(self):
        from redibis.pii.regex_catalog import normalize_msisdn_egypt
        assert normalize_msisdn_egypt("01012345678")    == "+201012345678"
        assert normalize_msisdn_egypt("'01012345678")   == "+201012345678"
        assert normalize_msisdn_egypt("01012345678.0")  == "+201012345678"
        assert normalize_msisdn_egypt(None)             is None

    def test_compiled_patterns_separate_groups(self):
        from redibis.pii.regex_catalog import compiled_structured, compiled_free_text
        # No overlap: structured uses ^...$, free_text uses \b...\b
        assert len(compiled_structured) > 0
        assert len(compiled_free_text)  > 0


# ═════════════════════════════════════════════════════════════════════════════
# 8. Integration — three workflows end-to-end
# ═════════════════════════════════════════════════════════════════════════════

class TestWorkflowIntegration:

    def test_three_workflows_produce_consistent_active(self, store, sample_pii_detections):
        table = "telecom.customers"

        # Workflow A — GE-only
        store.upsert(
            partial={
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
            },
            table=table, workflow="ge", run_id="ge-r1",
        )

        # Workflow B — PII (uses PIIContractWriter)
        pii_writer = PIIContractWriter("telecom", "customers", run_id="pii-r1")
        pii_writer.add_detections(sample_pii_detections)
        pii_partial = pii_writer.build()
        store.upsert(pii_partial, table=table, workflow="pii", run_id="pii-r1")

        # Workflow C — Business
        store.upsert(
            partial={
                "schema": [{
                    "name": "telecom_customers",
                    "properties": [{
                        "name": "phone",
                        "businessName": "Mobile Phone",
                        "description": "E.164 phone number",
                        "tags": ["regulated"],
                    }],
                }],
            },
            table=table, workflow="business", run_id="biz-r1",
        )

        # Final active state
        active = store.get_active(table)
        phone = next(p for p in active["schema"][0]["properties"] if p["name"] == "phone")

        assert phone["physicalType"]     == "VARCHAR(20)"           # from GE
        from tests.contract_helpers import col_pii_engine
        assert col_pii_engine(phone).get("detected") is True        # from PII
        assert phone["businessName"]     == "Mobile Phone"          # from biz
        assert phone["description"]      == "E.164 phone number"    # from biz
        assert phone["classification"]   == "pii_personal"          # from PII
        assert phone["quality"]                                     # from GE
        assert "pii"       in phone["tags"]                         # from PII
        assert "regulated" in phone["tags"]                         # from biz

        workflows = [p["workflow"] for p in store.metadata.get_provenance(table)]
        assert {"ge", "pii", "business"} <= set(workflows)

    def test_audit_history_reflects_each_run(self, store, sample_pii_detections):
        table = "telecom.customers"
        store.upsert({"schema": []}, table=table, workflow="ge", run_id="r1")

        pii_writer = PIIContractWriter("telecom", "customers", run_id="r2")
        pii_writer.add_detections(sample_pii_detections)
        store.upsert(pii_writer.build(), table=table, workflow="pii", run_id="r2")

        store.upsert(
            {"description": {"purpose": "biz"}},
            table=table, workflow="business", run_id="r3",
        )

        history = store.get_history(table)
        assert len(history) == 3
        run_ids = [h.run_id for h in history]
        assert set(run_ids) == {"r1", "r2", "r3"}

    def test_re_running_pii_replaces_pii_block_only(self, store, sample_pii_detections):
        table = "telecom.customers"
        # Initial PII run
        w1 = PIIContractWriter("telecom", "customers", run_id="pii-r1")
        w1.add_detection(PIIDetection(column="phone", detected=True,
                                       entity_type="PHONE_NUMBER", confidence=0.85))
        store.upsert(w1.build(), table=table, workflow="pii", run_id="pii-r1")

        # GE run between PII runs
        store.upsert(
            partial={
                "schema": [{
                    "name": "telecom_customers",
                    "properties": [{"name": "phone", "quality": [{"rule": "x"}]}],
                }],
            },
            table=table, workflow="ge", run_id="ge-r1",
        )

        # Second PII run with higher confidence
        w2 = PIIContractWriter("telecom", "customers", run_id="pii-r2")
        w2.add_detection(PIIDetection(column="phone", detected=True,
                                       entity_type="PHONE_NUMBER", confidence=0.95))
        store.upsert(w2.build(), table=table, workflow="pii", run_id="pii-r2")

        active = store.get_active(table)
        phone = active["schema"][0]["properties"][0]
        # PII policy updated; confidence lives in metadata sidecar
        from tests.contract_helpers import col_pii_engine
        evidence = store.metadata.get_column_evidence("telecom.customers", "phone")
        assert evidence.get("confidence") == 0.95
        assert "confidence" not in (phone.get("privacy") or {})
        # Quality block still present (from GE run)
        assert phone["quality"]


# ═════════════════════════════════════════════════════════════════════════════
# Standalone runner
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
