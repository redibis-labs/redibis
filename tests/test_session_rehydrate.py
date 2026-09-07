"""Session rehydration from scan_output on disk."""

import io
import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_rehydrate_storage")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_rehydrate_output")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_rehydrate_configs")


@pytest.fixture
def client():
    from redibis.webapp.backend import app
    return TestClient(app)


def test_get_session_rehydrates_from_disk(client):
    csv = b"email,age\na@b.com,30\n"
    r = client.post(
        "/api/sessions",
        files={"file": ("t.csv", io.BytesIO(csv), "text/csv")},
        data={"table": "telecom.customers"},
    )
    assert r.status_code == 200
    session_id = r.json()["session_id"]

    from redibis.services.session_service import session_manager
    session = session_manager.get_session(session_id)
    session.status = "scan_complete"
    session.pii_detected_count = 1
    session.total_columns = 2
    session.persist_to_disk()

    session_manager.delete_session(session_id)
    reloaded = session_manager.get_session(session_id)
    assert reloaded is not None
    assert reloaded.status == "scan_complete"

    r = client.get(f"/api/sessions/{session_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == session_id
    assert body["table_name"] == "telecom.customers"
    assert body["status"] == "scan_complete"
    assert body["pii_detected_count"] == 1

    state_path = os.path.join(os.environ["SCAN_OUTPUT_DIR"], session_id, "session.json")
    assert os.path.exists(state_path)


def test_list_sessions_includes_disk_only(client):
    csv = b"name\nx\n"
    r = client.post(
        "/api/sessions",
        files={"file": ("t.csv", io.BytesIO(csv), "text/csv")},
        data={"table": "disk.only"},
    )
    session_id = r.json()["session_id"]

    from redibis.services.session_service import session_manager
    session = session_manager.get_session(session_id)
    session.persist_to_disk()
    session_manager.delete_session(session_id)

    r = client.get("/api/sessions")
    assert r.status_code == 200
    ids = {s["session_id"] for s in r.json()}
    assert session_id in ids
