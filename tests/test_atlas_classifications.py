"""Tests for Atlas atomic classification push extension."""

from __future__ import annotations

from pathlib import Path

import yaml

from redibis.classification import ClassificationService, get_builtin_pack
from redibis.services.catalog.atlas import build_atlas_plan, plan_to_preview
from redibis.services.catalog.base import CatalogPushOptions

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
TABLE = "telecom.customers"


def test_atlas_plan_includes_classification_payloads():
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    svc = ClassificationService(get_builtin_pack("telecom"))
    results = svc.classify_contract(contract, TABLE)
    plan = build_atlas_plan(
        contract, TABLE, classification_results=results,
    )
    assert plan.classification_payloads
    type_names = {p["typeName"] for p in plan.classification_payloads}
    assert "PII" in type_names or "MSISDN" in type_names


def test_atlas_preview_has_atomic_step():
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    svc = ClassificationService(get_builtin_pack("telecom"))
    results = svc.classify_contract(contract, TABLE)
    plan = build_atlas_plan(contract, TABLE, classification_results=results)
    preview = plan_to_preview(plan, CatalogPushOptions())
    class_steps = [s for s in preview["steps"] if s.get("target") == "classifications"]
    assert class_steps
    assert class_steps[0].get("atomic") is True
    assert class_steps[0].get("verify_readback") is True
