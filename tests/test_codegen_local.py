"""Tests for local codegen service and default submit_codegen routing."""

import pytest
from redibis.agents.codegen import submit_codegen
from redibis.agents.codegen_local.service import LocalCodegenService
from redibis.agents.codegen_local.validators import validate_ranger


def test_local_codegen_service_ranger_template():
    svc = LocalCodegenService()
    contract = {
        "columns": {
            "cust_id": {"pii": "CUSTOMER_ID"},
            "email": {"pii": "EMAIL"},
        }
    }
    result = svc.generate(
        intent="Mask customer identifiers",
        table="telecom.customers",
        contract=contract,
        target_system="ranger",
    )
    assert result["status"] == "proposed"
    assert result["generation_source"] == "template"
    assert "proposal" in result
    assert result["vuln_report"]["passed"] is True
    assert result["judge_verdict"]["approved"] is True

    validation = validate_ranger(result["code"])
    assert validation.passed is True


def test_submit_codegen_local_default():
    contract = {
        "columns": {
            "phone": {"pii": "PHONE_NUMBER"},
        }
    }
    res = submit_codegen(
        intent="Mask phone column",
        table="telecom.customers",
        contract=contract,
        target_system="ranger",
    )
    assert res["status"] == "proposed"
    assert "code" in res
    assert "policies" in res["code"]
    assert res["judge_verdict"]["approved"] is True
    assert res.get("method") == "local"


def test_template_ranger_policy_from_summary_columns():
    from redibis.agents.codegen_local.service import template_ranger_policy

    src = template_ranger_policy(
        table="telecom.customers",
        intent="mask",
        columns=[{
            "name": "msisdn",
            "masking_policy": {"default_strategy": "fpe"},
            "entity_type": "PHONE_NUMBER",
        }],
        note="unit",
    )
    assert "redibis_telecom_customers_msisdn_mask" in src
    assert validate_ranger(src).passed is True


def test_registered_targets_includes_ranger():
    from redibis.agents.codegen_local.validators import registered_targets

    assert "ranger" in registered_targets()
