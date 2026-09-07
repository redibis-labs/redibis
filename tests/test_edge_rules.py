"""Acceptance tests for declarative edge-case classification rules."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
import yaml

from redibis.classification.edge_rules import (
    EdgeRuleColumnContext,
    apply_edge_rules,
    apply_edge_rules_to_detection,
    build_edge_context,
    merge_edge_rules,
    merge_policy_edge_rules,
    validate_edge_rules,
)
from redibis.classification.pack_store import (
    get_pack_text,
    is_user_pack,
    list_packs,
    load_pack,
    save_pack_text,
    user_pack_dir,
)
from redibis.classification.policy_pack import _parse_pack, get_builtin_pack
from redibis.config import ConfigError
from redibis.models import PIIDetection
from redibis.services import pipeline


@pytest.fixture
def telecom_policy():
    return get_builtin_pack("telecom")


def test_telecom_pack_loads_edge_rules(telecom_policy):
    ids = {r["id"] for r in telecom_policy.edge_rules}
    assert "custid_not_nid" in ids
    assert "latlon_is_location" in ids


def test_customer_id_nid_checksum_fail_demoted_to_not_pii(telecom_policy):
    detection = PIIDetection(
        column="customer_id",
        detected=True,
        entity_type="NATIONAL_ID",
        confidence=0.9,
    )
    df = pd.DataFrame({"customer_id": ["12345678901234", "99999999999999"]})
    updated, result = apply_edge_rules_to_detection(detection, telecom_policy, df=df)
    assert result.matched_rule_id == "custid_not_nid"
    assert updated.detected is False
    assert updated.entity_type is None


def test_valid_nid_in_customer_id_not_demoted(telecom_policy):
    detection = PIIDetection(
        column="customer_id",
        detected=True,
        entity_type="NATIONAL_ID",
        confidence=0.9,
    )
    df = pd.DataFrame({"customer_id": ["29001011401234", "29001011401235"]})
    updated, result = apply_edge_rules_to_detection(detection, telecom_policy, df=df)
    assert result.matched_rule_id is None
    assert updated.detected is True
    assert updated.entity_type == "NATIONAL_ID"


def test_numeric_lat_address_becomes_location(telecom_policy):
    detection = PIIDetection(
        column="lat",
        detected=True,
        entity_type="ADDRESS",
        confidence=0.85,
    )
    df = pd.DataFrame({"lat": [30.05, 31.2, 29.9]})
    updated, result = apply_edge_rules_to_detection(detection, telecom_policy, df=df)
    assert result.matched_rule_id == "latlon_is_location"
    assert updated.entity_type == "LOCATION"
    assert updated.detected is True


def test_text_address_column_unchanged(telecom_policy):
    detection = PIIDetection(
        column="address",
        detected=True,
        entity_type="ADDRESS",
        confidence=0.85,
    )
    df = pd.DataFrame({"address": ["123 Main St", "456 Oak Ave"]})
    updated, result = apply_edge_rules_to_detection(detection, telecom_policy, df=df)
    assert result.matched_rule_id is None
    assert updated.entity_type == "ADDRESS"


def test_unknown_when_key_rejected_at_parse():
    raw = {
        "version": "1.0.0",
        "name": "bad",
        "domains": {},
        "edge_rules": [
            {
                "id": "bad_rule",
                "when": {"eval": "1+1"},
                "then": {"set_entity": "NOT_PII"},
            }
        ],
    }
    with pytest.raises(ConfigError, match="unknown keys"):
        _parse_pack(raw)


def test_bad_regex_rejected_at_parse():
    raw = {
        "version": "1.0.0",
        "name": "bad",
        "domains": {},
        "edge_rules": [
            {
                "id": "bad_regex",
                "when": {"name_matches": "[invalid"},
                "then": {"set_entity": "NOT_PII"},
            }
        ],
    }
    with pytest.raises(ConfigError, match="invalid regex"):
        _parse_pack(raw)


def test_overlay_replaces_global_rule_by_id(telecom_policy):
    overlay = [
        {
            "id": "custid_not_nid",
            "when": {"name_matches": "(?i)customer_id", "entity": "NATIONAL_ID"},
            "then": {"set_entity": "PHONE_NUMBER", "note": "overlay test"},
        }
    ]
    policy = merge_policy_edge_rules(telecom_policy, overlay)
    ctx = EdgeRuleColumnContext(
        column="customer_id",
        entity="NATIONAL_ID",
        raw_entity="NATIONAL_ID",
        checksums={"nid_checksum": "fail"},
    )
    result = apply_edge_rules(ctx, policy)
    assert result.matched_rule_id == "custid_not_nid"
    assert result.set_entity == "PHONE_NUMBER"


def test_merge_edge_rules_order():
    base = [{"id": "a", "when": {"entity": "X"}, "then": {"note": "a"}}]
    overlay = [{"id": "b", "when": {"entity": "Y"}, "then": {"note": "b"}}]
    merged = merge_edge_rules(base, overlay)
    assert [r["id"] for r in merged] == ["a", "b"]


def test_save_builtin_creates_user_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_DIR", str(tmp_path))
    text = get_pack_text("telecom")
    path = save_pack_text("telecom", text)
    assert Path(path).is_file()
    assert is_user_pack("telecom")
    packaged = Path(__file__).parent.parent / "redibis/classification/packs/telecom.yaml"
    assert "edge_rules:" in packaged.read_text(encoding="utf-8")
    user_text = (tmp_path / "telecom.yaml").read_text(encoding="utf-8")
    assert "edge_rules:" in user_text


def test_pipeline_applies_edge_rules_after_decide_pii(telecom_policy):
    df = pd.DataFrame({
        "customer_id": ["12345678901234"] * 5,
        "lat": [30.1, 31.2, 29.5, 30.0, 31.1],
    })
    detections = pipeline.run_pii_detection(
        df,
        engines="regex",
        equation_mode="lenient",
        edge_rules_enabled=True,
        policy_pack="telecom",
    )
    by_col = {d.column: d for d in detections}
    if "customer_id" in by_col and by_col["customer_id"].entity_type == "NATIONAL_ID":
        assert by_col["customer_id"].detected is False


def test_validate_edge_rules_empty_id():
    with pytest.raises(ConfigError):
        validate_edge_rules([{"when": {"entity": "X"}, "then": {"note": "n"}}])


def test_promote_suggest_rule_shape():
    from redibis.classification.edge_rules import suggest_rule_from_correction

    rule = suggest_rule_from_correction(
        column="msisdn_ref",
        from_entity="PHONE_NUMBER",
        to_entity="NOT_PII",
        note="test",
    )
    assert rule["when"]["entity"] == "PHONE_NUMBER"
    assert rule["then"]["set_entity"] == "NOT_PII"
    assert "msisdn" in rule["when"]["name_matches"]


def test_load_pack_prefers_user_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_DIR", str(tmp_path))
    custom = {
        "version": "9.9.9",
        "name": "general",
        "domains": {},
        "edge_rules": [],
    }
    save_pack_text("general", yaml.safe_dump(custom))
    policy = load_pack("general")
    assert policy.version == "9.9.9"
    # Builtin API must stay on the packaged general pack (taxonomy / edge rules).
    builtin = get_builtin_pack("general")
    assert builtin.version != "9.9.9"
    assert any(r.get("id") for r in builtin.edge_rules)


def test_apply_edge_rules_test_context():
    ctx = EdgeRuleColumnContext(
        column="lat",
        entity="ADDRESS",
        raw_entity="ADDRESS",
        is_numeric=True,
        min_value=29.0,
        max_value=31.0,
    )
    policy = get_builtin_pack("telecom")
    result = apply_edge_rules(ctx, policy)
    assert result.matched_rule_id == "latlon_is_location"


# ---------------------------------------------------------------------------
# Stronger validation negative tests (Phase 0 T0.2)
# ---------------------------------------------------------------------------

def test_boolean_as_string_rejected():
    """is_numeric must be bool, not string 'true'."""
    with pytest.raises(ConfigError, match="boolean"):
        validate_edge_rules([
            {"id": "r1", "when": {"is_numeric": "true"}, "then": {"note": "x"}}
        ])


def test_rate_out_of_range_rejected():
    """valid_rate_gte must be in [0, 1]."""
    with pytest.raises(ConfigError, match=r"\[0, 1\]"):
        validate_edge_rules([
            {"id": "r1", "when": {"valid_rate_gte": 1.5}, "then": {"note": "x"}}
        ])


def test_rate_negative_rejected():
    with pytest.raises(ConfigError, match=r"\[0, 1\]"):
        validate_edge_rules([
            {"id": "r1", "when": {"cardinality_ratio_gte": -0.1}, "then": {"note": "x"}}
        ])


def test_in_range_wrong_order_rejected():
    """in_range lo must be <= hi."""
    with pytest.raises(ConfigError, match="lo.*hi|<="):
        validate_edge_rules([
            {"id": "r1", "when": {"in_range": [10.0, 1.0]}, "then": {"note": "x"}}
        ])


def test_in_range_not_two_elements_rejected():
    with pytest.raises(ConfigError, match=r"\[lo, hi\]"):
        validate_edge_rules([
            {"id": "r1", "when": {"in_range": [1.0]}, "then": {"note": "x"}}
        ])


def test_in_range_infinite_rejected():
    import math
    with pytest.raises(ConfigError, match="finite"):
        validate_edge_rules([
            {"id": "r1", "when": {"in_range": [float("-inf"), 1.0]}, "then": {"note": "x"}}
        ])


def test_tags_not_list_rejected():
    with pytest.raises(ConfigError):
        validate_edge_rules([
            {"id": "r1", "when": {"entity": "X"}, "then": {"add_tags": "not-a-list"}}
        ])


def test_tags_empty_string_rejected():
    with pytest.raises(ConfigError, match="non-empty string"):
        validate_edge_rules([
            {"id": "r1", "when": {"entity": "X"}, "then": {"add_tags": [""]}}
        ])


def test_tag_too_long_rejected():
    from redibis.classification.edge_rules import _MAX_TAG_LEN
    with pytest.raises(ConfigError, match="maximum tag length"):
        validate_edge_rules([
            {"id": "r1", "when": {"entity": "X"},
             "then": {"add_tags": ["x" * (_MAX_TAG_LEN + 1)]}}
        ])


def test_require_review_as_string_rejected():
    """require_review must be bool, not string."""
    with pytest.raises(ConfigError, match="boolean"):
        validate_edge_rules([
            {"id": "r1", "when": {"entity": "X"}, "then": {"require_review": "yes"}}
        ])


def test_rule_level_unknown_key_rejected():
    """Unknown keys at rule level (outside when/then) are rejected."""
    with pytest.raises(ConfigError, match="unknown keys"):
        validate_edge_rules([
            {
                "id": "r1",
                "when": {"entity": "X"},
                "then": {"note": "x"},
                "random_field": "surprise",
            }
        ])


def test_too_many_rules_rejected():
    from redibis.classification.edge_rules import _MAX_RULES
    rules = [{"id": f"r{i}", "when": {"entity": "X"}, "then": {"note": "n"}}
             for i in range(_MAX_RULES + 1)]
    with pytest.raises(ConfigError, match="maximum rule count"):
        validate_edge_rules(rules)


def test_valid_rules_pass_through():
    """Ensure valid rules are returned unchanged."""
    rules = [
        {
            "id": "valid-rule",
            "when": {
                "name_matches": "(?i)lac",
                "is_numeric": True,
                "cardinality_ratio_gte": 0.5,
                "null_rate_lte": 0.1,
            },
            "then": {
                "set_entity": "NETWORK_ID",
                "add_tags": ["telecom"],
                "require_review": False,
                "note": "Network identifier.",
            },
        }
    ]
    result = validate_edge_rules(rules)
    assert len(result) == 1
    assert result[0]["id"] == "valid-rule"


def test_overlay_rejected_when_not_allowed(monkeypatch):
    """pipeline.run_pii_detection raises ConfigError if overlay not allowed."""
    from redibis.config import RedibisConfig

    class _FakeClassification:
        edge_rules_enabled = True
        policy_pack = "telecom"
        per_run_overlay_allowed = False

    class _FakeCfg:
        classification = _FakeClassification()

    monkeypatch.setattr(RedibisConfig, "load", classmethod(lambda cls: _FakeCfg()))

    df = pd.DataFrame({"col": ["val"]})
    with pytest.raises(ConfigError, match="per_run_overlay_allowed"):
        pipeline.run_pii_detection(
            df,
            engines="regex",
            edge_rules_enabled=True,
            edge_rules_overlay=[{"id": "x", "when": {"entity": "X"}, "then": {"note": "y"}}],
        )
