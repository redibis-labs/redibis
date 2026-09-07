"""API tests for session load endpoints."""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_load_api_storage")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_load_api_configs")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_output"))
    from redibis.webapp.backend import app
    return TestClient(app)


def test_session_sources(client):
    r = client.get("/api/session-sources")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "scan" in names
    assert "agent" in names


def test_load_session_happy_path(client):
    csv = b"email,age\na@b.com,30\n"
    r = client.post(
        "/api/sessions",
        files={"file": ("t.csv", io.BytesIO(csv), "text/csv")},
        data={"table": "telecom.customers"},
    )
    session_id = r.json()["session_id"]

    from redibis.services.session_service import session_manager
    session = session_manager.get_session(session_id)
    session.persist_to_disk()
    session_manager.delete_session(session_id)

    r = client.get("/api/sessions/available?source=scan")
    assert r.status_code == 200
    assert any(s["session_id"] == session_id for s in r.json()["sessions"])

    r = client.post("/api/sessions/load", json={"run_id": session_id, "source": "scan"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == session_id
    assert body["loaded"] is True
    assert body["summary"]["table_name"] == "telecom.customers"

    r = client.get(f"/api/sessions/{session_id}")
    assert r.status_code == 200


def test_load_session_404(client):
    r = client.post("/api/sessions/load", json={"run_id": "does-not-exist", "source": "scan"})
    assert r.status_code == 404


def test_load_session_bad_source(client):
    r = client.post("/api/sessions/load", json={"run_id": "abc", "source": "nope"})
    assert r.status_code == 400


def test_load_session_bad_run_id(client):
    r = client.post("/api/sessions/load", json={"run_id": "../etc", "source": "scan"})
    assert r.status_code == 400
