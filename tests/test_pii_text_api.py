"""API tests for /api/pii/text/*."""

from __future__ import annotations

import logging
import os

import pytest

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_pii_text_api")

from fastapi.testclient import TestClient

from redibis.webapp.backend import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    r = client.get("/api/pii/text/health")
    assert r.status_code == 200
    body = r.json()
    assert body["offset_unit"] == "unicode_codepoint"
    assert "engines" in body


def test_entities(client):
    r = client.get("/api/pii/text/entities")
    assert r.status_code == 200
    body = r.json()
    assert body["offset_unit"] == "unicode_codepoint"
    assert isinstance(body["entities"], list)
    assert body["entities"]


def test_policies(client):
    r = client.get("/api/pii/text/policies")
    assert r.status_code == 200
    assert "policies" in r.json()


def test_scan_email(client):
    r = client.post("/api/pii/text/scan", json={
        "text": "Contact alice@example.com please",
        "engines": "regex",
        "min_score": 0.2,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["offset_unit"] == "unicode_codepoint"
    assert "spans" in body
    emails = [s for s in body["spans"] if "EMAIL" in s["entity_type"]]
    assert emails
    s = emails[0]
    assert "alice@example.com" in (s.get("text") or "")
    assert "is_proposal" in s


def test_scan_return_text_false(client):
    r = client.post("/api/pii/text/scan", json={
        "text": "alice@example.com",
        "engines": "regex",
        "return_text": False,
        "min_score": 0.2,
    })
    assert r.status_code == 200
    for s in r.json()["spans"]:
        assert s.get("text") == ""


def test_scan_oversized(client):
    r = client.post("/api/pii/text/scan", json={
        "text": "x" * 100,
        "max_chars": 50,
    })
    assert r.status_code == 413


def test_scan_no_raw_body_in_info_logs(client, caplog):
    secret = "unique-secret-token-xyz-999@example.com"
    with caplog.at_level(logging.INFO, logger="services.text_pii"):
        r = client.post("/api/pii/text/scan", json={"text": secret, "engines": "regex", "min_score": 0.2})
    assert r.status_code == 200
    joined = " ".join(r.message for r in caplog.records)
    assert secret not in joined


def test_deidentify_fail_closed(client):
    r = client.post("/api/pii/text/deidentify", json={
        "text": "alice@example.com",
        "policy_id": "does-not-exist",
    })
    assert r.status_code == 400


def test_deidentify_full_redact(client):
    r = client.post("/api/pii/text/deidentify", json={
        "text": "mail alice@example.com now",
        "engines": "regex",
        "min_score": 0.2,
        "policy_id": "full-redact",
    })
    assert r.status_code == 200
    body = r.json()
    assert "deidentified_text" in body
    assert "master_key" not in body
    assert "seed" not in body
    assert "run_key_ref" in body
    if body.get("spans"):
        assert "alice@example.com" not in body["deidentified_text"]


def test_deidentify_inline_policy(client):
    r = client.post("/api/pii/text/deidentify", json={
        "text": "mail alice@example.com now",
        "engines": "regex",
        "min_score": 0.2,
        "policy": {
            "id": "inline",
            "default": {"entity_type": "*", "strategy": "redact", "replacement": "[X]"},
        },
    })
    assert r.status_code == 200
    assert "[X]" in r.json()["deidentified_text"] or "EMAIL" in r.json()["deidentified_text"]
