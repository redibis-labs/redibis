"""E5/E6 — commercial content layout + enterprise module discovery."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_oss_reference_classification_packs_remain():
    packs = ROOT / "redibis" / "classification" / "packs"
    assert (packs / "telecom.yaml").is_file()
    assert (packs / "general.yaml").is_file()


def test_oss_free_agentic_course_remains():
    course = ROOT / "docs" / "agents" / "course"
    if not course.is_dir():
        pytest.skip("public documentation is intentionally excluded from this export")
    assert course.is_dir()
    assert any(course.glob("*.md"))


def test_enterprise_content_example_manifest_schema():
    path = (
        ROOT
        / "enterprise"
        / "content"
        / "packs"
        / "example-telecom-eu"
        / "0.0.0"
        / "content.yaml"
    )
    if not path.is_file():
        pytest.skip("enterprise content tree not present (OSS export)")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["apiVersion"] == "redibis.io/content/v1"
    assert doc["kind"] == "CommercialPackRelease"
    assert doc["sku"]
    assert doc["version"]
    assert doc["license"] == "proprietary"
    assert "compatibility" in doc
    assert doc["compatibility"]["pack_api"] == "redibis.io/pack/v1"


def test_discover_enterprise_modules_has_no_secrets():
    from redibis.enterprise import discover_enterprise_modules, enterprise_status_payload

    modules = discover_enterprise_modules()
    assert {m["id"] for m in modules} >= {
        "reports",
        "codegen_service",
        "codegen_plugin",
        "commercial_content",
        "airgap_deploy",
    }
    for mod in modules:
        for key in mod:
            assert key not in {"api_key", "password", "secret", "token", "private_key"}
        for val in mod.values():
            if isinstance(val, str):
                assert "BEGIN PRIVATE" not in val
                assert "sk-" not in val
    payload = enterprise_status_payload()
    assert "modules" in payload
    assert "docs" in payload


def test_enterprise_status_api_route_registered():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from redibis.webapp.backend import app

    res = TestClient(app).get("/api/enterprise/status")
    assert res.status_code == 200
    body = res.json()
    assert "modules" in body
    assert isinstance(body["modules"], list)
    assert "docs" in body
