"""Tests for NEREnsemble — multi-model × multi-label-group matrix."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from redibis.pii.ner_backend import NERHit, NERReport, NERResult
from redibis.pii.ner_ensemble import NEREnsemble


@dataclass
class _StubBackend:
    name: str
    labels: list[str] = field(default_factory=lambda: ["A", "B"])
    _responses: dict[tuple[str, ...], list[NERHit]] = field(default_factory=dict)

    def analyze(self, values, column_name, *, labels=None):
        key = tuple(labels or self.labels)
        hits = self._responses.get(key, [])
        return NERReport(
            model=self.name,
            labels_requested=list(labels or self.labels),
            hits=hits,
        )

    def score_values(self, values, column_name, *, labels=None) -> NERResult:
        return self.analyze(values, column_name, labels=labels).to_result()

    def health_check(self) -> dict:
        return {"loadable": True}


def test_run_column_model_times_groups():
    m1 = _StubBackend("m1", _responses={("A",): [NERHit("a", 0.8, 1.0)]})
    m2 = _StubBackend("m2", _responses={("B",): [NERHit("b", 0.7, 1.0)]})
    ensemble = NEREnsemble([m1, m2])
    passes = ensemble.run_column(["x"], "col", label_groups=[["A"], ["B"]])
    assert len(passes) == 4
    assert {tuple(p.labels) for p in passes} == {("A",), ("B",)}
    assert {p.model for p in passes} == {"m1", "m2"}


def test_from_config_falls_back_to_single_model(monkeypatch):
    from redibis.config import PIIConfig, RedibisConfig

    cfg = RedibisConfig()
    cfg.pii = PIIConfig()
    stub = _StubBackend("single")
    monkeypatch.setattr(
        "redibis.pii.ner_registry.NERModelRegistry.try_load",
        lambda **kw: stub,
    )
    ensemble = NEREnsemble.from_config(cfg.pii)
    assert len(ensemble.backends) == 1
    assert ensemble.backends[0].name == "single"


def test_from_config_loads_many_models(monkeypatch):
    from redibis.config import NERConfig, PIIConfig, RedibisConfig
    from redibis.pii.ner_registry import NERModelRegistry, NERModelSpec

    cfg = RedibisConfig()
    cfg.pii = PIIConfig(ner=NERConfig(models=[
        {"type": "gliner", "path": "/m1"},
        {"type": "gliner", "path": "/m2"},
    ]))

    def fake_load(spec, **kw):
        return _StubBackend(f"gliner:{spec.path}")

    def fake_health(self):
        return {"loadable": True}

    monkeypatch.setattr(NERModelRegistry, "load", fake_load)
    monkeypatch.setattr(NERModelRegistry, "spec_from_path", lambda path, **kw: NERModelSpec(
        type="gliner", path=path, labels=["person"],
    ))
    monkeypatch.setattr(_StubBackend, "health_check", fake_health)
    ensemble = NEREnsemble.from_config(cfg.pii)
    assert len(ensemble.backends) == 2


def test_from_config_isolates_load_failures(monkeypatch):
    from redibis.config import NERConfig, PIIConfig, RedibisConfig
    from redibis.pii.ner_registry import NERModelRegistry, NERModelSpec

    cfg = RedibisConfig()
    cfg.pii = PIIConfig(ner=NERConfig(models=[
        {"type": "gliner", "path": "/ok"},
        {"type": "gliner", "path": "/bad"},
    ]))

    def fake_load(spec, **kw):
        if "bad" in spec.path:
            raise FileNotFoundError("missing")
        return _StubBackend(f"gliner:{spec.path}")

    monkeypatch.setattr(NERModelRegistry, "load", fake_load)
    monkeypatch.setattr(NERModelRegistry, "spec_from_path", lambda path, **kw: NERModelSpec(
        type="gliner", path=path,
    ))
    ensemble = NEREnsemble.from_config(cfg.pii)
    assert len(ensemble.backends) == 1
    assert len(ensemble.unavailable) == 1


def test_load_many_isolates_failures(monkeypatch):
    from redibis.pii.ner_registry import NERModelRegistry, NERModelSpec

    specs = [
        NERModelSpec(type="gliner", path="/ok"),
        NERModelSpec(type="gliner", path="/bad"),
    ]
    calls = {"n": 0}

    def fake_instantiate(spec, **kw):
        calls["n"] += 1
        if "bad" in spec.path:
            raise FileNotFoundError("missing")
        return _StubBackend(spec.path)

    monkeypatch.setattr(NERModelRegistry, "_instantiate_backend", fake_instantiate)
    backends = NERModelRegistry.load_many(specs)
    assert len(backends) == 1
