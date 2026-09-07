import pytest
import pandas as pd
from redibis.models import PIIDetection
from redibis.pii.sensitivity import classify_sensitivity
from redibis.contracts.privacy import column_is_pii
from redibis.masking.plan import suggest_rule
from redibis.classification.policy_pack import get_builtin_pack
from redibis.classification.edge_rules import apply_edge_rules_to_detection
from redibis.pii.equations import build_column_report

def test_social_profile_url():
    policy = get_builtin_pack("general")
    det = PIIDetection(column="social_profile_url", detected=True, entity_type="URL")
    refined, _ = apply_edge_rules_to_detection(det, policy)
    
    assert refined.entity_type == "SOCIAL_PROFILE_URL"
    assert classify_sensitivity(refined.entity_type) == "pii_personal"
    
    rule = suggest_rule(refined.column, refined.entity_type, detected=refined.detected)
    assert rule.strategy == "fake"

def test_indirect_pii():
    policy = get_builtin_pack("general")
    
    # 1. session_id
    det_sess = PIIDetection(column="session_id", detected=True, entity_type=None)
    refined_sess, _ = apply_edge_rules_to_detection(det_sess, policy)
    assert refined_sess.entity_type == "SESSION_ID"
    assert classify_sensitivity(refined_sess.entity_type) == "pii_indirect"
    
    # 2. eshop_customer_id
    df = pd.DataFrame({"eshop_customer_id": ["C123", "C456"]})
    det_cust = PIIDetection(column="eshop_customer_id", detected=True, entity_type=None)
    refined_cust, _ = apply_edge_rules_to_detection(det_cust, policy, df=df)
    
    assert refined_cust.entity_type == "PSEUDO_ID"
    assert classify_sensitivity(refined_cust.entity_type) == "pii_indirect"

    # column_is_pii, tags, and contract masking
    from redibis.pii.contract_writer import PIIContractWriter
    from redibis.contracts.masking_policy import build_masking_policy

    for refined in (refined_sess, refined_cust):
        prop = {
            "name": refined.column,
            "privacy": {
                "classification": classify_sensitivity(refined.entity_type),
                "classification_engine": {
                    "detected": refined.detected,
                    "entity_type": refined.entity_type
                }
            }
        }
        assert column_is_pii(prop) is True
        
        rule = suggest_rule(refined.column, refined.entity_type, detected=refined.detected)
        assert rule.strategy in ("fpe", "passthrough")
        assert rule.strategy != "fake"

        writer = PIIContractWriter("db", "t")
        writer.add_detection(refined)
        tags = writer._build_tags(refined)
        assert "pii_indirect" in tags
        assert "gdpr_personal_data" in tags

        pol = build_masking_policy(
            refined.entity_type, detected=True,
            classification=classify_sensitivity(refined.entity_type),
        )
        assert pol["default"] == "fpe"

def test_security_sensitive():
    policy = get_builtin_pack("general")
    from redibis.pii.contract_writer import PIIContractWriter
    from redibis.contracts.masking_policy import build_masking_policy
    
    for col in ("password_hash", "csrf_token"):
        det = PIIDetection(column=col, detected=True, entity_type=None)
        refined, _ = apply_edge_rules_to_detection(det, policy)
        
        assert classify_sensitivity(refined.entity_type) == "security_sensitive"
        
        prop = {
            "name": refined.column,
            "privacy": {
                "classification": classify_sensitivity(refined.entity_type),
                "classification_engine": {
                    "detected": refined.detected,
                    "entity_type": refined.entity_type
                }
            }
        }
        assert column_is_pii(prop) is False
        
        rule = suggest_rule(refined.column, refined.entity_type, detected=refined.detected)
        assert rule.strategy in ("redact", "drop")
        assert rule.strategy != "fake"

        pol = build_masking_policy(
            refined.entity_type, detected=True,
            classification="security_sensitive",
        )
        assert pol["default"] == "redact"
        
        report = build_column_report(refined)
        assert report.advisory is not None
        assert "consider dropping" in report.advisory

        writer = PIIContractWriter("db", "t")
        writer.add_detection(refined)
        telemetry = writer.build_column_telemetry()
        assert col in telemetry
        assert telemetry[col].get("advisory") == report.advisory

def test_account_created_at_not_pii():
    policy = get_builtin_pack("general")
    det = PIIDetection(column="account_created_at", detected=False, entity_type=None)
    refined, _ = apply_edge_rules_to_detection(det, policy)
    
    assert refined.detected is False
    assert refined.entity_type is None
    
    prop = {
        "name": refined.column,
        "privacy": {
            "classification": classify_sensitivity(refined.entity_type),
            "classification_engine": {
                "detected": refined.detected,
                "entity_type": refined.entity_type
            }
        }
    }
    assert column_is_pii(prop) is False

def test_organization_is_non_pii():
    """An organization/brand reference (e.g. 'customer owns a Samsung device')
    is internal data about a company, not personal data about the customer."""
    from redibis.pii.contract_writer import PIIContractWriter
    from redibis.contracts.masking_policy import build_masking_policy

    det = PIIDetection(column="device_manufacturer", detected=True, entity_type="ORGANIZATION")
    assert classify_sensitivity(det.entity_type) == "internal"

    prop = {
        "name": det.column,
        "entity_type": det.entity_type,
        "privacy": {"classification": classify_sensitivity(det.entity_type)},
    }
    assert column_is_pii(prop) is False

    rule = suggest_rule(det.column, det.entity_type, detected=det.detected)
    assert rule.strategy == "passthrough"

    writer = PIIContractWriter("db", "t")
    writer.add_detection(det)
    assert writer._build_tags(det) == []
    assert writer._build_masking_policy(det) is None

    pol = build_masking_policy(det.entity_type, detected=True, classification="internal")
    assert pol["default"] == "passthrough"


def test_employer_is_indirect_pii():
    """Unlike a bare organization reference, an employer/workplace column
    indirectly identifies the specific person who works there."""
    policy = get_builtin_pack("general")
    det = PIIDetection(column="employer_name", detected=False, entity_type=None)
    refined, result = apply_edge_rules_to_detection(det, policy)

    assert result.matched_rule_id == "employer_is_indirect_pii"
    assert refined.entity_type == "EMPLOYER"
    assert refined.detected is True
    assert classify_sensitivity(refined.entity_type) == "pii_indirect"

    prop = {
        "name": refined.column,
        "entity_type": refined.entity_type,
        "privacy": {"classification": classify_sensitivity(refined.entity_type)},
    }
    assert column_is_pii(prop) is True

    rule = suggest_rule(refined.column, refined.entity_type, detected=refined.detected)
    assert rule.strategy == "fpe"


def test_device_manufacturer_and_company_reference_not_pii():
    """A device brand ('Samsung') or generic company reference must stay
    non-PII even when an NER engine mislabels the value as a PERSON."""
    policy = get_builtin_pack("general")

    for column in ("device_manufacturer", "phone_brand", "company_name", "vendor_name"):
        det = PIIDetection(
            column=column, detected=True, entity_type="PERSON",
            gliner_score=0.95, gliner_label="person",
        )
        refined, result = apply_edge_rules_to_detection(det, policy)

        assert result.matched_rule_id == "organization_reference_not_pii", column
        assert refined.entity_type == "ORGANIZATION"
        assert classify_sensitivity(refined.entity_type) == "internal"

        prop = {
            "name": refined.column,
            "entity_type": refined.entity_type,
            "privacy": {"classification": classify_sensitivity(refined.entity_type)},
        }
        assert column_is_pii(prop) is False

        rule = suggest_rule(refined.column, refined.entity_type, detected=refined.detected)
        assert rule.strategy == "passthrough"


def test_precedence_checksum_valid_id():
    policy = get_builtin_pack("general")
    
    # Valid Egypt national ID format: 29810150102583
    df = pd.DataFrame({"customer_id": ["29810150102583"]})
    det = PIIDetection(column="customer_id", detected=True, entity_type="NATIONAL_ID")
    
    refined, _ = apply_edge_rules_to_detection(det, policy, df=df)
    assert refined.entity_type == "NATIONAL_ID"
    assert classify_sensitivity(refined.entity_type) == "pii_sensitive"


def test_api_key_and_jwt_edge_rules():
    policy = get_builtin_pack("general")
    from redibis.contracts.masking_policy import build_masking_policy

    cases = [
        ("api_key", "API_KEY"),
        ("client_secret", "SECRET"),
        ("jwt_token", "JWT"),
        ("session_token", "SESSION_TOKEN"),
    ]
    for col, expected_entity in cases:
        det = PIIDetection(column=col, detected=True, entity_type=None)
        refined, _ = apply_edge_rules_to_detection(det, policy)
        assert refined.entity_type == expected_entity, col
        assert classify_sensitivity(refined.entity_type) == "security_sensitive"
        pol = build_masking_policy(
            refined.entity_type, detected=True,
            classification="security_sensitive",
        )
        assert pol["default"] == "redact"


def test_pii_summary_separates_security_from_pii():
    from redibis.pii.contract_writer import PIIContractWriter

    writer = PIIContractWriter("db", "t")
    writer.add_detection(PIIDetection(
        column="session_id", detected=True, entity_type="SESSION_ID",
    ))
    writer.add_detection(PIIDetection(
        column="password_hash", detected=True, entity_type="PASSWORD_HASH",
    ))
    summary = writer._build_pii_summary()
    assert summary["pii_columns"] == ["session_id"]
    assert summary["security_sensitive_columns"] == ["password_hash"]
    assert summary["pii_confirmed"] == 1
    assert summary["highest_sensitivity"] == "pii_indirect"


def test_pii_summary_security_only_highest():
    from redibis.pii.contract_writer import PIIContractWriter

    writer = PIIContractWriter("db", "t")
    writer.add_detection(PIIDetection(
        column="csrf_token", detected=True, entity_type="CSRF_TOKEN",
    ))
    summary = writer._build_pii_summary()
    assert summary["pii_columns"] == []
    assert summary["security_sensitive_columns"] == ["csrf_token"]
    assert summary["highest_sensitivity"] == "security_sensitive"
