"""CLI tests for ``redibis models``."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from redibis.cli.main import main
from redibis.pii.ner_registry import MANIFEST_FILENAME


def test_models_list_empty(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(tmp_path / "models"))
    code = main(["models", "list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "models_dir:" in out
    assert "no models" in out.lower()


def test_models_list_json(capsys, monkeypatch, tmp_path):
    models_dir = tmp_path / "models"
    model = models_dir / "demo"
    model.mkdir(parents=True)
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")
    (model / MANIFEST_FILENAME).write_text(json.dumps({"type": "gliner", "name": "demo"}), encoding="utf-8")
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(models_dir))

    code = main(["models", "list", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["models"]) == 1
    assert data["models"][0]["name"] == "demo"


def test_models_upload_and_activate_config(capsys, monkeypatch, tmp_path):
    models_dir = tmp_path / "models"
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(models_dir))
    cfg_path = tmp_path / "redibis.yaml"
    cfg_path.write_text("table: t.t\npii:\n  engines: both\n", encoding="utf-8")

    archive = tmp_path / "m.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("my-ner/gliner_config.json", "{}")
        zf.writestr("my-ner/model.safetensors", b"x")
        zf.writestr(f"my-ner/{MANIFEST_FILENAME}", json.dumps({"type": "gliner", "name": "my-ner"}))
    archive.write_bytes(buf.getvalue())

    with patch("redibis.pii.model_upload.smoke_inference", return_value=[]), \
         patch("redibis.pii.ner_backend.GLiNERBackend._ensure_loaded"):
        code = main(["models", "upload", str(archive), "--name", "my-ner"])
    assert code == 0

    with patch("redibis.pii.ner_backend.GLiNERBackend._ensure_loaded"):
        code = main(["models", "activate", "my-ner", "--config", str(cfg_path)])
    assert code == 0
    import yaml

    loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert loaded["pii"]["ner"]["model_path"] == str(models_dir / "my-ner")


def test_models_list_uses_config_models_dir(capsys, monkeypatch, tmp_path):
    team_dir = tmp_path / "team-a-models"
    model = team_dir / "demo"
    model.mkdir(parents=True)
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")
    (model / MANIFEST_FILENAME).write_text(json.dumps({"type": "gliner", "name": "demo"}), encoding="utf-8")

    cfg_path = tmp_path / "redibis.yaml"
    cfg_path.write_text("pii:\n  models_dir: " + str(team_dir).replace("\\", "/") + "\n", encoding="utf-8")
    monkeypatch.delenv("REDIBIS_MODELS_DIR", raising=False)

    code = main(["models", "list", "--config", str(cfg_path), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["models_dir"] == str(team_dir.resolve())
    assert len(data["models"]) == 1


def test_models_delete(capsys, monkeypatch, tmp_path):
    models_dir = tmp_path / "models"
    model = models_dir / "old"
    model.mkdir(parents=True)
    (model / "gliner_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(models_dir))

    code = main(["models", "delete", "old", "--yes"])
    assert code == 0
    assert not model.exists()
    assert "deleted" in capsys.readouterr().out
