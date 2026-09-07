"""Tests for CONTRACT_LIFECYCLE delta gaps (G2, G3, G5, G6)."""

import json

import pytest
import yaml

from redibis.contracts.lifecycle import (
    merge_pii_overlay_changes,
    parse_pii_changes_from_delta,
    resolve_enrichment_overlay_changes,
)
from redibis.enrich.evidence import assemble_column_evidence, load_run_detection_index
from redibis.services.catalog.lifecycle_artifacts import load_latest_lifecycle_artifacts
from redibis.services.catalog.openmetadata import attach_lifecycle_artifacts_to_odcs
from redibis.store.run_output_writer import RunOutputWriter
from redibis.store.storage_backend import LocalBackend


def test_parse_pii_changes_from_delta_promotion_and_demotion():
    c_det = {"schema": [{"properties": [
        {"name": "email", "classification": "pii_personal",
         "privacy": {"classification_engine": {"entity_type": "EMAIL"}}},
        {"name": "status", "classification": "pii_personal"},
    ]}]}
    delta = {
        "columns": {
            "email": {"pii": {"classification": "none"}},
            "phone": {"pii": {"classification": "pii_personal", "entity_type": "PHONE_NUMBER"}},
        },
    }
    changes = parse_pii_changes_from_delta(c_det, delta)
    by_col = {c["column"]: c for c in changes}
    assert by_col["email"]["status"] == "not_pii"
    assert by_col["phone"]["status"] == "pii"
    assert by_col["phone"]["entity_type"] == "PHONE_NUMBER"


def test_resolve_enrichment_overlay_merges_explicit_and_contract_diff():
    c_det = {"schema": [{"properties": [
        {"name": "email", "classification": "pii_personal",
         "privacy": {"classification_engine": {"entity_type": "EMAIL"}}},
    ]}]}
    candidate = {"schema": [{"properties": [
        {"name": "email", "classification": "none"},
    ]}]}
    explicit = [{"column": "email", "status": "not_pii"}]
    merged = resolve_enrichment_overlay_changes(
        c_det, candidate, explicit_changes=explicit, delta={},
    )
    assert len(merge_pii_overlay_changes(merged)) == 1
    assert merged[0]["status"] == "not_pii"


def test_assemble_column_evidence_includes_hits_and_profile():
    prop = {
        "name": "msisdn",
        "logicalType": "string",
        "quality": [{"type": "expect_column_values_to_not_be_null"}],
        "privacy": {"classification_engine": {"entity_type": "PHONE_NUMBER"}},
    }
    telemetry = {"confidence": 0.9, "decision_rule": "phone_engine"}
    run_det = {
        "regex_hits": [{"pattern_name": "eg_msisdn", "score": 0.95, "match_rate": 0.8}],
        "ner_hits": [{"label": "phone", "score": 0.7}],
    }
    ev = assemble_column_evidence("msisdn", prop, telemetry, run_detection=run_det)
    assert ev["regex_hits"][0]["pattern_name"] == "eg_msisdn"
    assert ev["ner_hits"]
    assert ev["profile"]["quality_rule_count"] == 1
    assert ev["edge_rule_verdict"] == "phone_engine"


def test_load_run_detection_index_from_scan_run(tmp_path):
    backend = LocalBackend(tmp_path)
    bucket = "runs"
    writer = RunOutputWriter(
        backend=backend, bucket=bucket, workflow="scan",
        table="telecom.customers", run_id="2026-01-01_12-00-00",
    )
    writer.write("pii_detections.json", [{
        "column": "email",
        "regex_hits": [{"pattern_name": "email", "score": 0.9}],
        "ner_hits": [],
    }])
    idx = load_run_detection_index(backend, bucket, "telecom.customers")
    assert "email" in idx
    assert idx["email"]["regex_hits"]


def test_load_latest_lifecycle_artifacts(tmp_path):
    backend = LocalBackend(tmp_path)
    bucket = "runs"
    writer = RunOutputWriter(
        backend=backend, bucket=bucket, workflow="enrich",
        table="telecom.customers", run_id="enrich_run_1",
    )
    writer.write("contract_diff.md", "# diff\n")
    writer.write("contract_diff.json", {"summary": {"overrides": 1}})
    writer.write("contract.active.ref.json", {"source": "llm", "run_id": "enrich_run_1"})
    art = load_latest_lifecycle_artifacts(backend, bucket, "telecom.customers")
    assert art["diff_md"].startswith("# diff")
    assert art["active_ref"]["source"] == "llm"


def test_attach_lifecycle_artifacts_to_odcs():
    contract = {"apiVersion": "v3.0.1", "kind": "DataContract", "schema": []}
    out = attach_lifecycle_artifacts_to_odcs(contract, {
        "diff_md": "## LLM changed",
        "active_ref": {"source": "llm", "run_id": "r1", "det_version": "1.0.0"},
        "diff_json": {"summary": {"overrides": 2, "adds": 1, "unchanged": 3}},
    })
    props = {p["property"]: p["value"] for p in out["customProperties"]}
    assert "redibis_contract_diff_md" in props
    assert props["redibis_active_source"] == "llm"
    assert "overrides=2" in props["redibis_diff_summary"]


def test_hint_to_dict_includes_score_and_prior_decision():
    from redibis.memory.retriever import RetrievedContext, hint_to_dict
    from redibis.memory.decision import ReviewDecision

    ctx = RetrievedContext(
        fingerprint_summary={"table": "telecom.customers", "name_normalized": "email"},
        decisions=[ReviewDecision(
            "fp:email", "telecom.customers", "email",
            pii_verdict="pii", classification="pii_personal",
        )],
        similarity=0.87,
    )
    row = hint_to_dict(ctx)
    assert row["table_column"] == "telecom.customers.email"
    assert row["score"] == 0.87
    assert row["prior_decision"] == "pii_personal"
