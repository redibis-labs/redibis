"""Tests for classification policy pack + engine."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redibis.classification import (
    CandidateTag,
    ClassificationContext,
    ClassificationService,
    JurisdictionContext,
    PolicyEngine,
    get_builtin_pack,
    list_builtin_packs,
)
from redibis.classification.atlas_bridge import atlas_classifications_preview
from redibis.services.catalog.mapping import load_contract_file

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
TABLE = "telecom.customers"


@pytest.fixture
def contract() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def policy():
    return get_builtin_pack("telecom")


def test_list_builtin_packs():
    packs = list_builtin_packs()
    assert "telecom" in packs


def test_policy_pack_domains(policy):
    assert "DataSensitivity" in policy.domains
    assert "DataSecurity" in policy.domains
    assert "PII" in policy.domain_tags("DataSensitivity")


def test_us_jurisdiction_cpni_from_regulated_tag(contract, policy):
    svc = ClassificationService(policy)
    svc.set_jurisdiction(JurisdictionContext(table=TABLE, jurisdiction="US"))
    regulated_contract = {
        **contract,
        "schema": [{
            **contract["schema"][0],
            "properties": [
                *contract["schema"][0]["properties"],
                {"name": "billing_notes", "logicalType": "string", "tags": ["regulated"]},
            ],
        }],
    }
    result = svc.classify_column(regulated_contract, TABLE, "billing_notes")
    assert "DataSensitivity:CPNI" in result.tag_keys()


def test_msisdn_classification(contract, policy):
    svc = ClassificationService(policy)
    svc.set_jurisdiction(JurisdictionContext(table=TABLE, jurisdiction="EU"))
    result = svc.classify_column(contract, TABLE, "msisdn")
    keys = result.tag_keys()
    assert "TelcoDataType:MSISDN" in keys
    assert "DataSensitivity:PII" in keys
    assert "DataSecurity:Confidential" in keys
    assert "RegulatoryCompliance:GDPR" in keys
    assert "PrivacyState:RawPersonalData" in keys
    assert result.approval_role in ("Steward", "DPO", "Architecture", "Legal")


def test_national_id_restricted(contract, policy):
    svc = ClassificationService(policy)
    result = svc.classify_column(contract, TABLE, "national_id")
    keys = result.tag_keys()
    assert "DataSensitivity:SubscriberPII" in keys
    assert "DataSecurity:Confidential" in keys


def test_lawful_intercept_escalation(policy):
    engine = PolicyEngine(policy)
    candidates = [
        CandidateTag(
            domain="RegulatoryCompliance",
            tag="LawfulIntercept",
            confidence=0.9,
            source="test",
        ),
    ]
    result = engine.resolve(candidates, ClassificationContext(table=TABLE, column="x"))
    assert result.escalations
    assert "LawfulIntercept" not in result.tag_keys()
    assert result.approval_role == "SecOps"


def test_golden_rule_single_data_security(policy):
    engine = PolicyEngine(policy)
    candidates = [
        CandidateTag(domain="DataSecurity", tag="Public", confidence=1.0, source="test"),
        CandidateTag(domain="DataSecurity", tag="Internal", confidence=1.0, source="test"),
    ]
    result = engine.resolve(candidates, ClassificationContext(table=TABLE))
    assert any("single_data_security" in v for v in result.violations)


def test_atlas_preview_atomic(contract, policy):
    svc = ClassificationService(policy)
    results = svc.classify_contract(contract, TABLE)
    preview = atlas_classifications_preview(results)
    assert preview["atomic"] is True
    assert "msisdn" in preview["columns"]
    assert preview["verify_readback"] is True


def test_classify_cli_fixture(contract, tmp_path):
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    loaded = load_contract_file(path)
    svc = ClassificationService()
    results = svc.classify_contract(loaded, TABLE)
    assert len(results) >= 2
