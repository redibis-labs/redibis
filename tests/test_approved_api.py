"""FastAPI tests for /api/sessions/{sid}/approved routes."""

import io
import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_approved_api_storage")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_approved_api_output")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_approved_api_configs")


@pytest.fixture
def client():
    from redibis.webapp.backend import app
    return TestClient(app)


@pytest.fixture
def session_id(client):
    csv = b"email,age\na@b.com,30\n"
    r = client.post("/api/sessions", files={"file": ("t.csv", io.BytesIO(csv), "text/csv")},
                    data={"table": "telecom.customers"})
    assert r.status_code == 200
    return r.json()["session_id"]


def test_approved_crud_and_merge(client, session_id, tmp_path):
    det = {"column": "email", "detected": True, "entity_type": "EMAIL_ADDRESS",
           "confidence": 0.95, "presidio_score": 0.9}
    r = client.post(f"/api/sessions/{session_id}/approved/pii",
                    json={"column": "email", "detection": det, "source": "scan"})
    assert r.status_code == 200
    prop = r.json()
    assert prop["kind"] == "pii"
    assert prop["column"] == "email"
    assert prop["payload"]["name"] == "email"

    r = client.post(f"/api/sessions/{session_id}/approved/quality",
                    json={"column": "age", "rule": {"expectation_type": "expect_column_values_to_not_be_null"}})
    assert r.status_code == 200
    assert r.json()["kind"] == "quality"

    from redibis.services.session_service import session_manager
    session_manager.delete_session(session_id)
    r = client.get(f"/api/sessions/{session_id}/approved")
    assert r.status_code == 200
    assert r.json()["summary"]["quality"] == 1

    r = client.get(f"/api/sessions/{session_id}/approved")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["total"] == 2
    assert len(body["items"]) == 2

    pid = prop["prop_id"]
    r = client.put(f"/api/sessions/{session_id}/approved/{pid}", json={"note": "checked"})
    assert r.status_code == 200
    assert r.json()["note"] == "checked"

    r = client.get(f"/api/sessions/{session_id}/approved/preview")
    assert r.status_code == 200
    assert r.json()["partials"]["pii"] is not None

    r = client.post(f"/api/sessions/{session_id}/approved/merge",
                    json={"validate_contract": False})
    assert r.status_code == 200
    merged = r.json()
    assert merged["status"] == "merged"
    assert merged["merged_version"]

    from redibis.services.session_service import session_manager
    session = session_manager.get_session(session_id)
    session.persist_to_disk()
    approved_path = os.path.join(os.environ["SCAN_OUTPUT_DIR"], session_id, "approved.json")
    assert os.path.exists(approved_path)
    on_disk = json.load(open(approved_path, encoding="utf-8"))
    assert on_disk["summary"]["merged"] == 2

    r = client.delete(f"/api/sessions/{session_id}/approved/{pid}")
    assert r.status_code == 200
    assert r.json()["summary"]["total"] == 1
