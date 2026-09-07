"""PII catalogue and NER inventory export."""

from __future__ import annotations

import json
import os

import pytest
import yaml

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_pii_export_storage")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_pii_export_output")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_pii_export_configs")

from redibis.pii.export import (
    export_regex_catalog,
    export_ner_models,
    list_regex_catalog_summary,
)
from redibis.pii.regex_catalog import CATALOG
from redibis.pii.regex_overrides import RegexOverrides


def test_export_regex_catalog_json_includes_pattern_and_entity():
    text = export_regex_catalog(fmt="json")
    rows = json.loads(text)
    assert len(rows) >= len([e for e in CATALOG.values() if e.active])
    sample = next(r for r in rows if r["name"] == "msisdn_egypt_any_format")
    assert sample["pattern"]
    assert sample["entity_type"] == "PHONE_NUMBER"


def test_export_regex_catalog_active_only_filters():
    rows = export_regex_catalog(active_only=True, fmt="list")
    assert all(r["active"] for r in rows)


def test_export_regex_catalog_yaml_and_csv():
    yml = export_regex_catalog(fmt="yaml")
    parsed = yaml.safe_load(yml)
    assert isinstance(parsed, list)
    assert parsed[0]["name"]

    csv_text = export_regex_catalog(fmt="csv")
    assert "name" in csv_text.splitlines()[0]
    assert "entity_type" in csv_text.splitlines()[0]


def test_list_regex_catalog_summary():
    rows = list_regex_catalog_summary(active_only=True)
    assert rows
    assert "name" in rows[0]
    assert "entity_type" in rows[0]


def test_export_with_overrides_remove():
    overrides = RegexOverrides(remove=["msisdn_egypt_any_format"])
    rows = export_regex_catalog(overrides=overrides, fmt="list")
    names = {r["name"] for r in rows}
    assert "msisdn_egypt_any_format" not in names


def test_export_ner_models_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(tmp_path))
    specs = export_ner_models()
    assert specs == []


@pytest.fixture
def client():
    from redibis.webapp.backend import app
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_api_pii_regex_overrides_round_trip(client):
    body = {
        "add": {
            "custom_test": {
                "pattern": r"\d{4}",
                "entity_type": "PHONE_NUMBER",
                "recognizer_group": "structured",
                "presidio_score": 0.8,
                "active": True,
            }
        },
        "remove": ["msisdn_egypt_vodafone"],
    }
    r = client.post("/api/pii/regex/overrides", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["overrides"]["add"]["custom_test"]["pattern"] == r"\d{4}"
    assert "msisdn_egypt_vodafone" in data["overrides"]["remove"]

    r2 = client.get("/api/pii/regex?format=json")
    assert r2.status_code == 200
    catalog = json.loads(r2.text)
    names = {row["name"] for row in catalog}
    assert "custom_test" in names
    assert "msisdn_egypt_vodafone" not in names


def test_api_pii_ner_labels(client):
    r = client.put("/api/pii/ner/labels", json={"labels": ["person", "iban"]})
    assert r.status_code == 200
    assert r.json()["labels"] == ["person", "iban"]

    r2 = client.get("/api/pii/ner/labels")
    assert r2.status_code == 200
    assert r2.json()["labels"] == ["person", "iban"]
    assert r2.json()["source"] == "config"


def test_api_pii_ner_labels_defaults_when_unset(client):
    from redibis.pii.ner_backend import DEFAULT_NER_LABELS
    from redibis.webapp.backend import get_config_store

    gs = get_config_store().load_global_settings()
    gs.pop("pii_ner_labels", None)
    get_config_store().save_global_settings(gs)

    r = client.get("/api/pii/ner/labels")
    assert r.status_code == 200
    body = r.json()
    assert body["labels"] == list(DEFAULT_NER_LABELS)
    assert body["source"] == "default"
