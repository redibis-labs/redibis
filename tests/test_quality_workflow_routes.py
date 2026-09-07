"""Web quality workflow routes — step endpoints, draft load, suppress-all."""

from __future__ import annotations

import io

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from redibis.webapp.backend import app
    return TestClient(app)


def _upload_session(client: TestClient) -> str:
    buf = io.BytesIO()
    pd.DataFrame({"id": [1, 2], "phone": ["555-0100", None]}).to_csv(buf, index=False)
    buf.seek(0)
    r = client.post(
        "/api/sessions",
        files={"file": ("sample.csv", buf, "text/csv")},
        data={"table": "data.sample", "scan_mode": "quality"},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_step_profile_and_quality_endpoints_exist(client):
    sid = _upload_session(client)
    r = client.post(f"/api/sessions/{sid}/step/profile")
    assert r.status_code == 200
    assert r.json()["step"] == "profile"

    r = client.post(f"/api/sessions/{sid}/step/quality")
    assert r.status_code == 200
    assert r.json()["step"] == "quality"


def test_draft_quality_upload_sets_rule_set(client):
    sid = _upload_session(client)
    rules = [{"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {}}]
    payload = yaml.safe_dump({"name": "test-rules", "rules": rules})
    r = client.post(
        f"/api/sessions/{sid}/draft/quality/upload",
        files={"file": ("rules.yaml", payload.encode(), "application/x-yaml")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "test-rules"
    assert len(body["rules"]) == 1


def test_save_quality_config_with_ge_code(client, tmp_path, monkeypatch):
    from redibis.webapp import backend

    from redibis.store.config_store import LocalConfigStore

    store_dir = tmp_path / "configs"
    monkeypatch.setattr(backend, "get_config_store", lambda: LocalConfigStore(store_dir))

    r = client.post(
        "/api/configs/quality",
        json={
            "name": "curated-v1",
            "description": "pasted",
            "rules": [{"rule": "expect_column_values_to_not_be_null", "column": "id"}],
            "ge_code": "qa.add_gx_expectation(expectation_name='expect_column_values_to_not_be_null', column='id')",
        },
    )
    assert r.status_code == 200, r.text
    doc = client.get("/api/configs/quality/curated-v1").json()
    assert doc["ge_code"].startswith("qa.add_gx_expectation")
    assert len(doc["rules"]) == 1


def test_suppress_all_quality_rules_route(client, monkeypatch):
    from redibis.webapp import backend

    class FakeStore:
        def suppress_all_quality_rules(self, table, *, decided_by="", run_id=""):
            class Res:
                version_after = "3"
            return Res()

    monkeypatch.setattr(backend, "get_contract_store", lambda: FakeStore())
    r = client.post(
        "/api/contracts/data.sample/quality-decisions/suppress-all",
        json={"decided_by": "test"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "suppressed_all"


def test_pii_regex_test_endpoint(client):
    sid = _upload_session(client)
    r = client.post(
        f"/api/sessions/{sid}/discovery/pii/test-regex",
        json={
            "pattern": r"^\d{3}-\d{4}$",
            "test_values": ["555-0100", "bad"],
            "column": "phone",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["match_count"] >= 1
    assert any(h["match"] for h in body["hits"])

    # Egyptian MSISDN: dashed input matches after digits-only cleanup
    r_msisdn = client.post(
        f"/api/sessions/{sid}/discovery/pii/test-regex",
        json={
            "pattern": r"^01[0125]\d{8}$",
            "test_values": ["010-1234-5678"],
            "normalizer": "normalize_msisdn_egypt",
        },
    )
    assert r_msisdn.status_code == 200, r_msisdn.text
    msisdn_body = r_msisdn.json()
    assert msisdn_body["match_count"] == 1
    assert msisdn_body["hits"][0]["tested_as"] == "digits_only"

    # Auto-sample first column with data when no values/column given
    r2 = client.post(
        f"/api/sessions/{sid}/discovery/pii/test-regex",
        json={"pattern": r".+"},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["total"] >= 1
    assert body2["sampled_column"] in ("id", "phone")
