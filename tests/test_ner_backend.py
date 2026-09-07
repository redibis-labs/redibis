"""Tests for NERBackend.analyze / NERReport / GLiNERBackend."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from redibis.pii.ner_backend import GLiNERBackend, NERBackend, NERHit, NERReport


class TestNERReport:
    def test_best_and_to_result(self):
        hits = [
            NERHit(label="phone number", score=0.7, match_rate=0.5),
            NERHit(label="person", score=0.9, match_rate=0.3),
        ]
        report = NERReport(model="test:m", labels_requested=["person", "phone number"], hits=hits)
        assert report.best().label == "person"
        result = report.to_result()
        assert result["score"] == 0.9
        assert result["label"] == "person"
        assert result["match_rate"] == 0.3


def test_gliner_analyze_multi_entity(monkeypatch):
    backend = GLiNERBackend("/fake/model", labels=["person", "phone number"])
    fake_model = MagicMock()
    fake_model.batch_predict_entities.return_value = [
        [
            {"label": "person", "score": 0.85},
            {"label": "phone number", "score": 0.72},
        ],
        [{"label": "person", "score": 0.91}],
    ]
    backend._model = fake_model
    monkeypatch.setattr(backend, "_ensure_loaded", lambda: None)

    report = backend.analyze(["Alice", "Bob"], "name")
    assert len(report.hits) == 2
    labels = {h.label for h in report.hits}
    assert "person" in labels
    assert "phone number" in labels
    assert report.best().label == "person"
    assert report.best().score == 0.91


def test_gliner_score_values_matches_analyze_to_result(monkeypatch):
    backend = GLiNERBackend("/fake/model", labels=["person"])
    fake_model = MagicMock()
    fake_model.batch_predict_entities.return_value = [
        [{"label": "person", "score": 0.88}],
    ]
    backend._model = fake_model
    monkeypatch.setattr(backend, "_ensure_loaded", lambda: None)

    via_analyze = backend.analyze(["Alice"], "name").to_result()
    via_score = backend.score_values(["Alice"], "name")
    assert via_score == via_analyze


def test_gliner_analyze_passes_custom_labels(monkeypatch):
    backend = GLiNERBackend("/fake/model", labels=["person"])
    fake_model = MagicMock()
    fake_model.batch_predict_entities.return_value = [[]]
    backend._model = fake_model
    monkeypatch.setattr(backend, "_ensure_loaded", lambda: None)

    backend.analyze(["x"], "col", labels=["PHONE_NUMBER"])
    fake_model.batch_predict_entities.assert_called_once()
    assert fake_model.batch_predict_entities.call_args[0][1] == ["phone number"]


def test_entity_to_gliner_phrase_canonical():
    from redibis.pii.ner_backend import entity_to_gliner_phrase, labels_to_gliner_phrases, model_slug

    assert entity_to_gliner_phrase("PHONE_NUMBER") == "phone number"
    assert entity_to_gliner_phrase("email") == "email"
    assert labels_to_gliner_phrases(["PHONE_NUMBER", "EMAIL_ADDRESS"]) == ["phone number", "email"]
    assert model_slug("gliner:/models/my-ner") == "gliner_models_my_ner"


def test_gliner_inherits_base_score_values(monkeypatch):
    backend = GLiNERBackend("/fake/model", labels=["person"])
    fake_model = MagicMock()
    fake_model.batch_predict_entities.return_value = [[{"label": "person", "score": 0.5}]]
    backend._model = fake_model
    monkeypatch.setattr(backend, "_ensure_loaded", lambda: None)

    from redibis.pii.ner_backend import BaseNERBackend

    assert isinstance(backend, BaseNERBackend)
    assert backend.score_values(["x"], "c") == backend.analyze(["x"], "c").to_result()


def test_gliner_is_ner_backend_protocol():
    backend = GLiNERBackend("/fake/model")
    assert isinstance(backend, NERBackend)


def test_missing_gliner_package_reports_install_hint(monkeypatch):
    """``gliner`` itself absent → the classic 'pip install' hint."""
    backend = GLiNERBackend("/fake/model")

    def _fail_import(name, *a, **kw):
        if name == "gliner":
            exc = ImportError("No module named 'gliner'")
            exc.name = "gliner"
            raise exc
        return real_import(name, *a, **kw)

    import builtins

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _fail_import)
    with pytest.raises(ImportError) as ei:
        backend._ensure_loaded()
    assert "pip install redibis[ner-runtime]" in str(ei.value)


def test_broken_transitive_dependency_reports_real_root_cause(monkeypatch):
    """``gliner`` installed but a dependency (e.g. torch) is broken — the
    real underlying error must surface, not a misleading 'install gliner'."""
    backend = GLiNERBackend("/fake/model")

    def _fail_import(name, *a, **kw):
        if name == "gliner":
            exc = ImportError("No module named 'torch._C'")
            exc.name = "torch._C"
            raise exc
        return real_import(name, *a, **kw)

    import builtins

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _fail_import)
    with pytest.raises(ImportError) as ei:
        backend._ensure_loaded()
    message = str(ei.value)
    assert "torch._C" in message
    assert "not a missing package" in message
    assert "pip install redibis[ner-runtime]" not in message


def test_health_check_reports_loadable_false_with_error(monkeypatch):
    backend = GLiNERBackend("/fake/model")
    monkeypatch.setattr(
        backend,
        "_ensure_loaded",
        MagicMock(side_effect=ImportError("gliner failed to import (No module named 'torch._C')")),
    )
    report = backend.health_check()
    assert report["loadable"] is False
    assert "torch._C" in report["error"]
    assert report["type"] == "gliner"


def test_health_check_reports_loadable_true_when_model_loads(monkeypatch):
    backend = GLiNERBackend("/fake/model")
    monkeypatch.setattr(backend, "_ensure_loaded", lambda: None)
    report = backend.health_check()
    assert report["loadable"] is True
    assert "error" not in report
