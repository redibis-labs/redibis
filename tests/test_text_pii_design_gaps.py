"""Acceptance gaps for free-text PII design (batch, aliases, pack, parity, guards)."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_pii_text_gaps")

from fastapi.testclient import TestClient

from redibis.masking.engine import MaskingEngine, RunKeys
from redibis.masking.plan import ColumnMaskRule, MaskingPlan
from redibis.pii import PIISpan, TextPIIScan, TextScanConfig, TextScanResult
from redibis.pii.deid.applier import DeidApplier
from redibis.pii.deid.pack_store import load_deid_policies, save_deid_policy
from redibis.pii.deid.policy import DEID_API_VERSION, DeidPolicy, EntityRule
from redibis.pii.scan.result import Detection, DetectionResult
from redibis.pii.text_llm import LlmTextRefiner
from redibis.services.text_pii_service import TextPIIService, TextPIIServiceError
from redibis.webapp.backend import app


@pytest.fixture
def client():
    return TestClient(app)


def test_design_aliases():
    assert PIISpan is Detection
    assert TextScanResult is DetectionResult
    from redibis.pii import text_scan

    assert text_scan.TextPIIScan is TextPIIScan


def test_scan_batch_api(client):
    r = client.post("/api/pii/text/scan-batch", json={
        "texts": ["alice@example.com", "bob@example.org"],
        "engines": "regex",
        "min_score": 0.2,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["offset_unit"] == "unicode_codepoint"
    assert len(body["results"]) == 2


def test_scan_batch_oversized(client):
    r = client.post("/api/pii/text/scan-batch", json={
        "texts": ["a"] * 3,
        "max_items": 2,
    })
    assert r.status_code == 413


def test_deid_rejects_resolve_all(client):
    r = client.post("/api/pii/text/deidentify", json={
        "text": "alice@example.com",
        "engines": "regex",
        "resolve": "all",
        "policy_id": "full-redact",
    })
    assert r.status_code == 400
    assert "all" in r.json()["detail"].lower()


def test_deid_policy_api_version_roundtrip(tmp_path):
    pol = DeidPolicy.redact_all("demo")
    d = pol.to_dict()
    assert d["apiVersion"] == DEID_API_VERSION
    yml = tmp_path / "demo.yaml"
    yml.write_text(pol.to_yaml(), encoding="utf-8")
    loaded = DeidPolicy.from_yaml(yml)
    assert loaded.id == "demo"
    assert loaded.api_version == DEID_API_VERSION


def test_pack_policy_save_load(tmp_path):
    pol = DeidPolicy(
        id="support-notes",
        default=EntityRule(entity_type="*", strategy="redact"),
        overrides=(EntityRule(entity_type="EMAIL_ADDRESS", strategy="mask", params={"keep_last": 4}),),
    )
    path = save_deid_policy(tmp_path, pol)
    assert path.exists()
    loaded = load_deid_policies(tmp_path)
    assert "support-notes" in loaded
    assert loaded["support-notes"].overrides[0].strategy == "mask"


def test_suggest_and_save_policy_api(client, monkeypatch, tmp_path):
    monkeypatch.setenv("REDIBIS_DEID_POLICIES_DIR", str(tmp_path))
    r = client.post("/api/pii/text/suggest-policy", json={
        "text": "mail alice@example.com",
        "engines": "regex",
        "min_score": 0.2,
        "policy_id": "suggested",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["apiVersion"] == DEID_API_VERSION
    body["id"] = "from-api"
    r2 = client.post("/api/pii/text/policies", json={"policy": body})
    assert r2.status_code == 200
    assert (tmp_path / "masking" / "policies" / "from-api.yaml").is_file()


def test_masking_engine_parity_on_span():
    value = "alice@example.com"
    text = f"xx {value} yy"
    start, end = 3, 3 + len(value)
    result = DetectionResult(
        kind="span",
        detections=(Detection("EMAIL_ADDRESS", 0.95, "regex", start, end, value),),
        entity_counts={"EMAIL_ADDRESS": 1},
        ruleset_id="t",
        ruleset_version="1",
        language="en",
    )
    keys = RunKeys.mint(seed="parity")
    policy = DeidPolicy(
        id="p",
        default=EntityRule(entity_type="*", strategy="hash", params={"algo": "sha256", "hmac_key_ref": "k1", "truncate": 12}),
    )
    out = DeidApplier().apply(text, result, policy, keys=keys)
    # Same transform via MaskingEngine on the isolated value
    plan = MaskingPlan(
        schema_table="t",
        columns=[ColumnMaskRule(column="v", strategy="hash", params={"algo": "sha256", "hmac_key_ref": "k1", "truncate": 12})],
        require_authenticated_crypto=False,
    )
    eng = MaskingEngine(plan, keys)
    series = pd.Series([value], name="v")
    working = pd.DataFrame({"v": series})
    transformed = str(eng._transform_series(series, plan.columns[0], working, None).iloc[0])
    assert out.deidentified_text == f"xx {transformed} yy"


def test_llm_blocks_cloud_provider():
    class Cfg:
        class pii:
            class llm:
                enabled = True
                provider = "openai"
                model_name = "gpt-4o"
                endpoint_url = "https://api.openai.com/v1"

    refiner = LlmTextRefiner(redibis_config=Cfg())
    with pytest.raises(RuntimeError, match="cloud LLM"):
        refiner._assert_local_provider("openai")


def test_service_save_policy(tmp_path):
    svc = TextPIIService(policies={"full-redact": DeidPolicy.redact_all()})
    pol = DeidPolicy.redact_all("saved-one")
    path = svc.save_policy(pol, root=tmp_path)
    assert Path(path).is_file()
    assert svc.get_policy("saved-one") is not None
