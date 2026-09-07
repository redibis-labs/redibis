"""Phase 6 portable pack API (/api/rdbpack/*) tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from redibis.pack import PackStackStore, export_default_pack


FIXTURES = Path(__file__).resolve().parent / "data" / "packs"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from redibis.webapp import pack_routes
    from redibis.webapp.backend import app

    stack_dir = tmp_path / "pack-stack"
    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(stack_dir))

    def _factory():
        return PackStackStore(root=stack_dir)

    pack_routes.register_pack_routes(app, store_factory=_factory)
    return TestClient(app), stack_dir


def test_stack_empty(client):
    c, stack_dir = client
    r = c.get("/api/rdbpack/stack")
    assert r.status_code == 200
    body = r.json()
    assert body["layers"] == []
    assert body["count"] == 0
    assert str(stack_dir) in body["stack_dir"]


def test_export_default_download(client):
    c, _ = client
    r = c.get("/api/rdbpack/export-default")
    assert r.status_code == 200
    assert "application/zip" in r.headers.get("content-type", "")
    assert r.content[:2] == b"PK"
    assert len(r.content) > 100


def test_validate_fixture_pack(client):
    c, _ = client
    path = FIXTURES / "fr-FR.rdbpack"
    with path.open("rb") as fh:
        r = c.post(
            "/api/rdbpack/validate",
            files={"file": ("fr-FR.rdbpack", fh, "application/zip")},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "fr" in body["identity"].lower() or "FR" in body["identity"]
    assert len(body["pack_sha256"]) == 64


def test_import_dry_run_then_confirm_and_remove(client, tmp_path):
    c, stack_dir = client
    pack_path = tmp_path / "default.rdbpack"
    export_default_pack(pack_path)

    with pack_path.open("rb") as fh:
        r = c.post(
            "/api/rdbpack/import",
            files={"file": ("default.rdbpack", fh, "application/zip")},
            data={"dry_run": "true", "activate": "false"},
        )
    assert r.status_code == 200, r.text
    dry = r.json()
    assert dry["dry_run"] is True
    assert dry["ok"] is True
    assert dry["identity"]
    assert dry["already_present"] is False
    assert "activate_note" in dry

    with pack_path.open("rb") as fh:
        r = c.post(
            "/api/rdbpack/import",
            files={"file": ("default.rdbpack", fh, "application/zip")},
            data={"dry_run": "false", "activate": "false"},
        )
    assert r.status_code == 200, r.text
    imported = r.json()
    assert imported["imported"] is True
    assert imported["dry_run"] is False
    identity = imported["identity"]

    r = c.get("/api/rdbpack/stack")
    assert r.status_code == 200
    layers = r.json()["layers"]
    assert len(layers) == 1
    assert f"{layers[0]['id']}@{layers[0]['version']}" == identity
    assert (stack_dir / "stack.json").is_file()

    r = c.post("/api/rdbpack/remove", json={"identity": identity})
    assert r.status_code == 200, r.text
    assert r.json().get("removed") or r.json().get("ok") is not False

    r = c.get("/api/rdbpack/stack")
    assert r.json()["layers"] == []


def test_remove_requires_identity(client):
    c, _ = client
    r = c.post("/api/rdbpack/remove", json={})
    assert r.status_code == 400


def test_empty_upload_rejected(client):
    c, _ = client
    r = c.post(
        "/api/rdbpack/import",
        files={"file": ("empty.rdbpack", b"", "application/zip")},
        data={"dry_run": "true"},
    )
    assert r.status_code == 400
