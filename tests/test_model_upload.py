"""Tests for secure NER model upload and API routes."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from redibis.pii.model_upload import (
    MAX_UPLOAD_BYTES,
    UploadResult,
    _safe_extract_path,
    delete_model,
    extract_archive,
    ingest_upload,
    list_models,
)
from redibis.pii.ner_registry import MANIFEST_FILENAME, NERModelSpec, ManifestReport


@pytest.fixture
def models_tmp(monkeypatch, tmp_path):
    root = tmp_path / "models"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(root))
    return root


def test_safe_extract_path_rejects_zip_slip(tmp_path):
    root = tmp_path / "dest"
    root.mkdir()
    with pytest.raises(ValueError, match="zip-slip|unsafe"):
        _safe_extract_path(root, "../etc/passwd")


def test_extract_archive_rejects_zip_slip(models_tmp, tmp_path):
    archive = tmp_path / "bad.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    archive.write_bytes(buf.getvalue())

    dest = tmp_path / "out"
    with pytest.raises(ValueError):
        extract_archive(archive, dest)


def test_extract_archive_rejects_decompression_bomb(models_tmp, tmp_path, monkeypatch):
    monkeypatch.setattr("redibis.pii.model_upload._extract_budget_bytes", lambda _: 10)
    archive = tmp_path / "bomb.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", b"hello world")
    archive.write_bytes(buf.getvalue())

    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="decompression budget"):
        extract_archive(archive, dest)


def test_extract_archive_valid_zip(models_tmp, tmp_path):
    model_dir_name = "my-model"
    archive = tmp_path / "model.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{model_dir_name}/gliner_config.json", "{}")
        zf.writestr(f"{model_dir_name}/{MANIFEST_FILENAME}", json.dumps({
            "type": "gliner",
            "name": "my-model",
            "labels": ["person"],
        }))
    archive.write_bytes(buf.getvalue())

    dest = tmp_path / "extracted"
    extract_archive(archive, dest)
    assert (dest / model_dir_name / "gliner_config.json").is_file()


def test_ingest_upload_rejects_oversized(models_tmp):
    result = ingest_upload(b"x" * (MAX_UPLOAD_BYTES + 1), "huge.zip")
    assert not result.ok
    assert "size cap" in result.errors[0]


def test_ingest_upload_rejects_readonly_models_dir(models_tmp, monkeypatch):
    root = models_tmp / "readonly_root"
    root.mkdir(parents=True)
    root.chmod(0o555)
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(root))
    try:
        result = ingest_upload(b"data", "tiny.zip", models_dir_base=str(root))
    finally:
        root.chmod(0o755)
    assert not result.ok
    assert any("not writable" in e for e in result.errors)


def test_ingest_upload_rejects_pickle(models_tmp, tmp_path):
    archive = tmp_path / "pickle.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("model/weights.pkl", b"pickle")
        zf.writestr("model/gliner_config.json", "{}")
    archive.write_bytes(buf.getvalue())

    with patch("redibis.pii.model_upload.NERModelRegistry.validate") as mock_val:
        mock_val.return_value = ManifestReport(
            ok=True,
            spec=NERModelSpec(type="gliner", path="x", name="model", labels=["person"]),
        )
        result = ingest_upload(archive.read_bytes(), "pickle.zip")
    assert not result.ok
    assert any("pickle" in e.lower() for e in result.errors)


def test_ingest_upload_promotes_valid_archive(models_tmp, tmp_path):
    archive = tmp_path / "good.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("arabic-pii/gliner_config.json", "{}")
        zf.writestr("arabic-pii/model.safetensors", b"fake-weights")
        zf.writestr(f"arabic-pii/{MANIFEST_FILENAME}", json.dumps({
            "type": "gliner", "name": "arabic-pii", "labels": ["person"],
        }))
    archive.write_bytes(buf.getvalue())

    spec = NERModelSpec(type="gliner", path="", name="arabic-pii", labels=["person"])
    with patch("redibis.pii.model_upload.NERModelRegistry.validate") as mock_val, \
         patch("redibis.pii.model_upload.smoke_inference", return_value=[]):
        mock_val.return_value = ManifestReport(ok=True, spec=spec)
        result = ingest_upload(archive.read_bytes(), "good.zip")

    assert result.ok, result.errors
    assert result.name == "arabic-pii"
    assert Path(result.path).is_dir()
    assert (Path(result.path) / "gliner_config.json").is_file()


def test_list_models_marks_active(models_tmp):
    model = models_tmp / "m1"
    model.mkdir(parents=True)
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")
    (model / MANIFEST_FILENAME).write_text(json.dumps({"type": "gliner", "name": "m1"}), encoding="utf-8")

    listing = list_models(active_path=str(model.resolve()))
    assert len(listing["models"]) == 1
    assert listing["models"][0]["active"] is True


def test_delete_model_removes_directory(models_tmp):
    model = models_tmp / "gone"
    model.mkdir(parents=True)
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")

    result = delete_model("gone")
    assert result["deleted"] is True
    assert not model.exists()


def test_delete_model_rejects_quarantine_name(models_tmp):
    with pytest.raises(ValueError, match="model name must match|reserved"):
        delete_model("_quarantine")


def test_delete_model_not_found(models_tmp):
    with pytest.raises(FileNotFoundError):
        delete_model("missing")


os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_models_api_storage")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_models_api_output")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_models_api_configs")


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from redibis.webapp.backend import app
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def session_id(client):
    csv = b"email,age\na@b.com,30\n"
    r = client.post(
        "/api/sessions",
        files={"file": ("t.csv", io.BytesIO(csv), "text/csv")},
        data={"table": "telecom.customers"},
    )
    assert r.status_code == 200
    return r.json()["session_id"]


def test_models_api_list_and_activate(client, session_id, models_tmp, monkeypatch):
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(models_tmp))
    model = models_tmp / "demo"
    model.mkdir()
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")

    with patch("redibis.services.model_service.activate_model") as mock_act:
        mock_act.return_value = (str(model.resolve()), {"name": "demo", "path": str(model), "type": "gliner", "labels": []})
        r = client.post(f"/api/sessions/{session_id}/models/activate", json={"name": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "activated"
    assert body["config"]["active_ner_model_name"] == "demo"

    r = client.get(f"/api/models?session_id={session_id}")
    assert r.status_code == 200


def test_models_api_list_uses_session_models_dir(client, session_id, tmp_path, monkeypatch):
    team_dir = tmp_path / "team-b"
    model = team_dir / "local"
    model.mkdir(parents=True)
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")

    from redibis.webapp.backend import _require_session

    session = _require_session(session_id)
    session.common_config.pii_models_dir = str(team_dir)
    session.persist_to_disk()

    monkeypatch.delenv("REDIBIS_MODELS_DIR", raising=False)
    r = client.get(f"/api/models?session_id={session_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["models_dir"] == str(team_dir.resolve())
    assert len(body["models"]) == 1


def test_models_api_upload(client, models_tmp, monkeypatch):
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(models_tmp))
    with patch("redibis.services.model_service.ingest_upload") as mock_ingest:
        mock_ingest.return_value = UploadResult(
            ok=True,
            name="x",
            path=str(models_tmp / "x"),
            spec={"type": "gliner"},
        )
        r = client.post(
            "/api/models/upload",
            files={"file": ("m.zip", io.BytesIO(b"fake"), "application/zip")},
            data={"name": "x"},
        )
    assert r.status_code == 200
    assert r.json()["name"] == "x"


def test_models_api_delete(client, models_tmp, monkeypatch):
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(models_tmp))
    model = models_tmp / "rm-me"
    model.mkdir()
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")

    r = client.delete("/api/models/rm-me")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert not model.exists()


def test_models_api_delete_not_found(client, models_tmp, monkeypatch):
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(models_tmp))
    r = client.delete("/api/models/nope")
    assert r.status_code == 404
