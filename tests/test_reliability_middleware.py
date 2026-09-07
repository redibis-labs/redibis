"""Phase 3: correlation id middleware and global error envelope."""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_output"))
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))

    from redibis.webapp.backend import app

    @app.get("/test-reliability-boom")
    def _boom():
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=False)


def test_correlation_id_on_500(client, caplog):
    caplog.set_level(logging.ERROR)
    res = client.get("/test-reliability-boom", headers={"x-request-id": "cid-abc"})
    assert res.status_code == 500
    body = res.json()
    assert body["error"] == "internal_error"
    assert body["correlation_id"] == "cid-abc"
    assert res.headers.get("x-request-id") == "cid-abc"


def test_http_exception_unchanged(client):
    from redibis.webapp.backend import app

    @app.get("/test-reliability-404")
    def _missing():
        raise HTTPException(status_code=404, detail="nope")

    res = client.get("/test-reliability-404")
    assert res.status_code == 404
    assert res.json()["detail"] == "nope"


def test_value_error_returns_500_envelope(client):
    from redibis.webapp.backend import app

    @app.get("/test-reliability-value-error")
    def _bad():
        raise ValueError("boom")

    res = client.get("/test-reliability-value-error")
    assert res.status_code == 500
    assert res.json()["error"] == "internal_error"
    assert res.json()["correlation_id"]
