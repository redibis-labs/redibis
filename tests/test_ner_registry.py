"""Tests for pluggable NER backends and registry."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.config import GlinerConfig, NERConfig, PIIConfig, RedibisConfig
from redibis.pii.detector import detect_pii
from redibis.pii.ner_backend import DEFAULT_NER_LABELS, GLiNERBackend, NERResult
from redibis.pii.ner_registry import NERModelRegistry
from redibis.pii.regex_overrides import RegexOverrides
from redibis.services import pipeline


@pytest.fixture
def mock_gliner():
    """Inject a fake ``gliner`` module so tests never import torch."""
    mod = MagicMock()
    with patch.dict(sys.modules, {"gliner": mod}):
        yield mod


class _FakeNERBackend:
    name = "fake:test"
    labels = ["person"]

    def __init__(self, score: float | None = 0.91, label: str = "phone number"):
        self._score = score
        self._label = label
        self.calls: list[dict] = []

    def analyze(self, values, column_name, *, labels=None):
        from redibis.pii.ner_backend import NERHit, NERReport
        active = list(labels or self.labels)
        self.calls.append({"labels": active, "column": column_name})
        if self._score is None:
            return NERReport(model=self.name, labels_requested=active)
        return NERReport(
            model=self.name,
            labels_requested=active,
            hits=[NERHit(label=self._label, score=self._score, match_rate=1.0)],
        )

    def score_values(self, values: list[str], column_name: str, *, labels=None) -> NERResult:
        return self.analyze(values, column_name, labels=labels).to_result()

    def health_check(self) -> dict:
        return {"type": "fake", "loadable": True}


@pytest.fixture(autouse=True)
def _clear_ner_cache():
    NERModelRegistry.clear_cache()
    yield
    NERModelRegistry.clear_cache()


def test_resolve_model_path_priority(monkeypatch, tmp_path):
    ner = NERConfig(model_path="/models/custom")
    gliner = GlinerConfig(model_id="legacy/id")
    assert NERModelRegistry.resolve_model_path(ner, gliner) == "/models/custom"

    ner = NERConfig(model_path="")
    monkeypatch.setenv("REDIBIS_NER_MODEL", "/models/from-env")
    assert NERModelRegistry.resolve_model_path(ner, gliner) == "/models/from-env"

    monkeypatch.delenv("REDIBIS_NER_MODEL", raising=False)
    with pytest.warns(DeprecationWarning, match="pii.gliner.model_id"):
        assert NERModelRegistry.resolve_model_path(ner, gliner) == "legacy/id"

    empty_root = tmp_path / "empty-models"
    empty_root.mkdir()
    assert NERModelRegistry.resolve_model_path(
        ner, GlinerConfig(model_id=""), models_dir=str(empty_root),
    ) is None


def test_resolve_model_path_auto_selects_sole_model(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    sole = root / "gliner-multi-v2.1"
    sole.mkdir()
    (sole / "gliner_config.json").write_text("{}", encoding="utf-8")

    ner = NERConfig(model_path="")
    assert NERModelRegistry.resolve_model_path(
        ner, GlinerConfig(model_id=""), models_dir=str(root),
    ) == str(sole)


def test_resolve_model_path_does_not_auto_select_multiple(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    for name in ("model-a", "model-b"):
        d = root / name
        d.mkdir()
        (d / "gliner_config.json").write_text("{}", encoding="utf-8")

    ner = NERConfig(model_path="")
    assert NERModelRegistry.resolve_model_path(
        ner, GlinerConfig(model_id=""), models_dir=str(root),
    ) is None


def test_spec_from_manifest(tmp_path):
    model_dir = tmp_path / "my-ner"
    model_dir.mkdir()
    (model_dir / "gliner_config.json").write_text("{}", encoding="utf-8")
    (model_dir / "redibis-model.json").write_text(json.dumps({
        "type": "gliner",
        "name": "arabic-pii",
        "labels": ["person", "national id"],
        "default_threshold": 0.35,
    }), encoding="utf-8")

    spec = NERModelRegistry.spec_from_path(str(model_dir))
    assert spec.type == "gliner"
    assert spec.name == "arabic-pii"
    assert spec.labels == ["person", "national id"]
    assert spec.default_threshold == 0.35


def test_spec_defaults_without_manifest(tmp_path):
    model_dir = tmp_path / "bare"
    model_dir.mkdir()
    (model_dir / "gliner_config.json").write_text("{}", encoding="utf-8")

    spec = NERModelRegistry.spec_from_path(str(model_dir))
    assert spec.type == "gliner"
    assert spec.labels == DEFAULT_NER_LABELS


def test_resolve_effective_labels_precedence(tmp_path):
    model_dir = tmp_path / "manifest-model"
    model_dir.mkdir()
    (model_dir / "redibis-model.json").write_text(
        json.dumps({"labels": ["iban", "swift"]}),
        encoding="utf-8",
    )

    labels, source = NERModelRegistry.resolve_effective_labels()
    assert labels == DEFAULT_NER_LABELS
    assert source == "default"

    labels, source = NERModelRegistry.resolve_effective_labels(stored=["person"])
    assert labels == ["person"]
    assert source == "config"

    labels, source = NERModelRegistry.resolve_effective_labels(model_path=str(model_dir))
    assert labels == ["iban", "swift"]
    assert source == "manifest"


def test_gliner_backend_local_files_only_missing_path(mock_gliner):
    backend = GLiNERBackend("/nonexistent/model/path")
    mock_gliner.GLiNER.from_pretrained.side_effect = OSError("missing")
    result = backend.score_values(["hello"], "col")
    assert result == {"score": None, "label": None, "match_rate": None}


def test_gliner_backend_loads_offline(tmp_path, mock_gliner):
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    fake_model = MagicMock()
    fake_model.batch_predict_entities.return_value = [[{"score": 0.88, "label": "person"}]]

    backend = GLiNERBackend(str(model_dir), labels=["person"])
    mock_gliner.GLiNER.from_pretrained.return_value = fake_model
    result = backend.score_values(["Alice"], "name")

    mock_gliner.GLiNER.from_pretrained.assert_called_once_with(
        str(model_dir), local_files_only=True,
    )
    assert result["score"] == 0.88
    assert result["label"] == "person"


def test_gliner_backend_uses_bundled_encoder(tmp_path, mock_gliner):
    model_dir = tmp_path / "local-model"
    encoder = model_dir / "encoder"
    encoder.mkdir(parents=True)
    (encoder / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "gliner_config.json").write_text(
        json.dumps({"model_name": "microsoft/mdeberta-v3-base"}),
        encoding="utf-8",
    )
    fake_model = MagicMock()
    fake_model.batch_predict_entities.return_value = [[{"score": 0.5, "label": "person"}]]

    backend = GLiNERBackend(str(model_dir), labels=["person"])
    mock_gliner.GLiNER.from_pretrained.return_value = fake_model
    backend.score_values(["Bob"], "name")

    mock_gliner.GLiNER.from_pretrained.assert_called_once_with(
        str(model_dir), local_files_only=True,
    )
    # Config restored after load
    restored = json.loads((model_dir / "gliner_config.json").read_text(encoding="utf-8"))
    assert restored["model_name"] == "microsoft/mdeberta-v3-base"


def test_registry_cache_respects_threshold_change(tmp_path):
    model_dir = tmp_path / "m"
    model_dir.mkdir()
    (model_dir / "gliner_config.json").write_text("{}", encoding="utf-8")
    spec = NERModelRegistry.spec_from_path(str(model_dir))

    with patch.object(NERModelRegistry.BACKENDS["gliner"], "__init__", autospec=True) as mock_init:
        mock_init.return_value = None
        NERModelRegistry.load(spec, threshold=0.3)
        NERModelRegistry.load(spec, threshold=0.7)
    assert mock_init.call_count == 2


def test_detect_pii_regex_only_without_ner_backend():
    df = pd.DataFrame({"phone": ["+201001234567"]})
    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {
            "score": 0.9,
            "pattern": "msisdn",
            "match_rate": 1.0,
            "entity_type": "PHONE",
        }
        detections = detect_pii(df, engines="ner", ner_backend=None)
    assert len(detections) == 1
    assert detections[0].gliner_score is None
    assert detections[0].ner_engine is None


def test_detect_pii_uses_ner_backend():
    df = pd.DataFrame({"name": ["Alice"]})
    backend = _FakeNERBackend()
    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {
            "score": None,
            "pattern": None,
            "match_rate": 0.0,
            "entity_type": None,
        }
        detections = detect_pii(
            df,
            engines="both",
            ner_backend=backend,
            gliner_always_run=True,
        )
    assert detections[0].gliner_score == 0.91
    assert detections[0].gliner_label == "phone number"
    assert detections[0].ner_engine == "fake:test"


def test_detect_pii_skips_ner_on_high_regex_score():
    df = pd.DataFrame({"phone": ["+201001234567"]})
    backend = _FakeNERBackend()
    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {
            "score": 0.95,
            "pattern": "msisdn",
            "match_rate": 1.0,
            "entity_type": "PHONE",
        }
        detections = detect_pii(df, engines="both", ner_backend=backend)
    assert detections[0].gliner_score is None


def test_detect_pii_accepts_ner_engine_alias():
    df = pd.DataFrame({"x": ["a"]})
    backend = _FakeNERBackend(score=0.5)
    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {"score": None, "pattern": None, "match_rate": 0.0, "entity_type": None}
        detections = detect_pii(df, engines="ner", ner_backend=backend, gliner_always_run=True)
    assert detections[0].gliner_score == 0.5


def test_detect_pii_forwards_regex_overrides():
    df = pd.DataFrame({"phone": ["123"]})
    overrides = RegexOverrides(
        add={
            "custom_pat": {
                "pattern": r"\d+",
                "entity_type": "PHONE_NUMBER",
                "recognizer_group": "structured",
                "presidio_score": 0.9,
            }
        }
    )
    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {
            "score": None, "pattern": None, "match_rate": 0.0, "entity_type": None,
        }
        detect_pii(df, engines="regex", regex_overrides=overrides)
    assert mock_presidio.call_args.kwargs.get("regex_overrides") is overrides


def test_pipeline_try_load_disabled_without_path(tmp_path, monkeypatch):
    empty_root = tmp_path / "empty-models"
    empty_root.mkdir()
    monkeypatch.setenv("REDIBIS_MODELS_DIR", str(empty_root))
    monkeypatch.delenv("REDIBIS_NER_MODEL", raising=False)
    df = pd.DataFrame({"a": ["x"]})
    with patch("redibis.services.pipeline.detect_pii") as mock_detect:
        mock_detect.return_value = []
        pipeline.run_pii_detection(
            df,
            engines="both",
            ner_config=NERConfig(model_path=""),
            gliner_config=GlinerConfig(model_id=""),
        )
    assert mock_detect.call_args.kwargs["ner_backend"] is None


def test_pipeline_loads_backend_from_config(tmp_path):
    df = pd.DataFrame({"a": ["x"]})
    fake = _FakeNERBackend()
    with patch.object(NERModelRegistry, "try_load", return_value=fake):
        with patch("redibis.services.pipeline.detect_pii") as mock_detect:
            mock_detect.return_value = []
            pipeline.run_pii_detection(
                df,
                ner_config=NERConfig(model_path=str(tmp_path / "m")),
            )
    assert mock_detect.call_args.kwargs["ner_backend"] is fake


def test_redibis_config_accepts_ner_block():
    cfg = RedibisConfig.from_dict({
        "pii": {
            "engines": "ner",
            "ner": {
                "model_path": "/models/custom",
                "labels": ["person"],
                "always_run": True,
            },
        },
    })
    assert cfg.pii.engines == "ner"
    assert cfg.pii.ner.model_path == "/models/custom"
    assert cfg.pii.ner.labels == ["person"]
    assert cfg.pii.ner_always_run() is True


def test_validate_accepts_ner_engine_choice():
    RedibisConfig.from_dict({"pii": {"engines": "ner"}}).validate()


def test_discover_skips_quarantine_and_hidden_dirs(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    (root / "_quarantine").mkdir()
    (root / "_quarantine" / "gliner_config.json").write_text("{}", encoding="utf-8")
    visible = root / "visible"
    visible.mkdir()
    (visible / "gliner_config.json").write_text("{}", encoding="utf-8")

    specs = NERModelRegistry.discover(root)
    assert len(specs) == 1
    assert specs[0].name == "visible"
