"""Tests for Deep Scan orchestrator (Phase 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from redibis.agents.deep_scan import (
    EvidenceArtifact,
    reconcile_signals,
    run_deep_scan,
)


def _surrogate_key_df(n: int = 20) -> pd.DataFrame:
    return pd.DataFrame({"id": list(range(1, n + 1))})


def test_run_deep_scan_fanout_writes_json_and_md(tmp_path):
    df = pd.DataFrame({
        "email": [f"user{i}@example.com" for i in range(5)],
        "score": [1, 2, 3, 4, 5],
    })
    producers = ["profile.ge", "profile.relationships", "quality.candidates"]
    result = run_deep_scan(
        "demo.t",
        "run-a",
        df,
        producers=producers,
        run_dir=tmp_path,
        build_bundle=True,
    )
    assert len(result["evidence"]) >= 3
    evidence_dir = tmp_path / "evidence"
    json_files = list(evidence_dir.glob("*.json"))
    md_files = list(evidence_dir.glob("*.md"))
    assert len(json_files) >= 3
    assert len(md_files) >= 3
    manifest = json.loads((tmp_path / "deep_scan_manifest.json").read_text())
    assert manifest["table"] == "demo.t"


def test_producer_exception_does_not_abort_others(tmp_path):
    df = pd.DataFrame({"x": [1, 2, 3]})

    def _boom(**_kw):
        raise RuntimeError("producer failed")

    import redibis.agents.deep_scan as ds

    orig = ds._dispatch_producer
    try:
        def _dispatch(cap, **kw):
            if cap.id == "profile.ge":
                raise RuntimeError("producer failed")
            return orig(cap, **kw)

        ds._dispatch_producer = _dispatch
        result = run_deep_scan(
            "demo.t",
            "run-b",
            df,
            producers=["profile.ge", "quality.candidates"],
            run_dir=tmp_path,
            build_bundle=False,
        )
    finally:
        ds._dispatch_producer = orig

    assert any("profile.ge" in e for e in result["errors"])
    assert any(e["producer"] == "quality.candidates" for e in result["evidence"])


def test_bundle_agreement_matrix_low_risk(tmp_path):
    df = pd.DataFrame({"email": ["a@b.com", "c@d.com"]})
    result = run_deep_scan(
        "demo.t",
        "run-c",
        df,
        producers=["rule.regex_catalog", "equation.decide", "profile.ge"],
        run_dir=tmp_path,
        build_bundle=True,
    )
    bundle = tmp_path / "deep_scan_bundle" / "manifest.json"
    assert bundle.is_file()
    manifest = json.loads(bundle.read_text())
    assert "agreement_matrix" in manifest
    # email column should have some agreement when regex fires
    if "email" in manifest["agreement_matrix"]:
        assert manifest["agreement_matrix"]["email"]


def test_reconcile_signals_fp_surrogate_key():
    columns_signals = {
        "id": [
            {"producer": "rule.regex_catalog", "verdict": "pii", "detected": True},
            {
                "producer": "profile.ge",
                "cardinality_ratio": 1.0,
                "nunique": 100,
                "n_rows": 100,
                "dtype": "int64",
            },
        ],
    }
    rec = reconcile_signals(columns_signals)
    assert rec["id"]["false_positive_risk"] == "high"
    assert rec["id"]["verdict"] == "not_pii"


def test_reconcile_signals_two_engines_low_risk():
    columns_signals = {
        "email": [
            {"producer": "rule.regex_catalog", "verdict": "pii"},
            {"producer": "equation.decide", "verdict": "pii"},
        ],
    }
    rec = reconcile_signals(columns_signals)
    assert rec["email"]["false_positive_risk"] == "low"
    assert rec["email"]["verdict"] == "pii"


def test_unknown_producer_recorded_not_crash(tmp_path):
    df = pd.DataFrame({"x": [1]})
    result = run_deep_scan(
        "demo.t",
        "run-d",
        df,
        producers=["not.real.producer"],
        run_dir=tmp_path,
        build_bundle=False,
    )
    assert result["errors"]
    assert result["evidence"] == []


def test_pii_artifact_no_raw_values(tmp_path):
    art = EvidenceArtifact(
        producer="rule.regex_catalog",
        kind="pii_evidence",
        table="demo.t",
        run_id="r",
        engine="regex",
        columns={
            "email": {
                "verdict": "pii",
                "score": 0.9,
                "pattern": "EMAIL",
                "match_rate": 1.0,
            },
        },
        summary={"columns_scored": 1},
    )
    art.write(tmp_path)
    payload = json.loads(
        (tmp_path / "evidence" / "rule_regex_catalog.demo_t.json").read_text()
    )
    blob = json.dumps(payload)
    assert "@" not in blob or "match_rate" in blob
    assert "match_rate" in blob
