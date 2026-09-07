"""Contract Synthesis — ODCS convert / validate / version profiles."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redibis.contracts.convert import convert_to_v31, normalize_portable_contract, propose_semantic_version_bump
from redibis.contracts.validate_odcs import assert_synthesis_contract, validate_odcs_contract
from redibis.contracts.versions import FUTURE_API_VERSION, SYNTHESIS_API_VERSION, get_profile

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthesis" / "cafc" / "02_data_contract.odcs.yaml"


def test_v32_profile_disabled():
    profile = get_profile(FUTURE_API_VERSION)
    assert profile.enabled is False
    with pytest.raises(RuntimeError, match="disabled"):
        profile.schema_path()


def test_convert_rule_to_metric_and_strip_telemetry():
    base = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    base["provenance"] = {"run_id": "x"}
    base["pii_summary"] = {"n": 1}
    out = convert_to_v31(base)
    assert out["apiVersion"] == SYNTHESIS_API_VERSION
    assert out["id"] == "cafc-customer-feature-composite-daily"
    assert "provenance" not in out
    assert "pii_summary" not in out
    q = out["schema"][0]["properties"][0]["quality"][0]
    assert q["metric"] == "nullValues"
    assert "rule" not in q
    assert q["mustBe"] == 0


def test_converted_cafc_is_schema_valid():
    base = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    out = convert_to_v31(base)
    errors = assert_synthesis_contract(out)
    assert errors == [], errors


def test_version_bump():
    assert propose_semantic_version_bump("1.0.0", kind="minor") == "1.1.0"
    assert propose_semantic_version_bump("1.2.3", kind="patch") == "1.2.4"
    assert propose_semantic_version_bump("1.2.3", kind="major") == "2.0.0"


def test_normalize_adds_default_operator():
    contract = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "x",
        "status": "draft",
        "version": "1.0.0",
        "schema": [{
            "name": "t",
            "logicalType": "object",
            "properties": [{
                "name": "a",
                "logicalType": "string",
                "quality": [{"type": "library", "metric": "nullValues"}],
            }],
        }],
        "servers": [{
            "server": "h",
            "type": "hive",
            "host": "h",
            "database": "d",
            "schema": "should_move",
        }],
    }
    normalize_portable_contract(contract)
    q = contract["schema"][0]["properties"][0]["quality"][0]
    assert q["mustBe"] == 0
    assert "schema" not in contract["servers"][0]
    props = {p["property"] for p in contract["servers"][0]["customProperties"]}
    assert "schema" in props
    result = validate_odcs_contract(contract, api_version="v3.1.0", strict=False)
    assert result["valid"], result["errors"]
