"""Deep scan NER ensemble producer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from redibis.agents.deep_scan import _produce_ner_gliner, run_deep_scan
from redibis.config import NERConfig, PIIConfig, RedibisConfig
from redibis.pii.ner_backend import NERHit, NERReport, NERResult


@dataclass
class _StubBackend:
    name: str
    labels: list[str] = field(default_factory=lambda: ["person"])

    def analyze(self, values, column_name, *, labels=None):
        active = list(labels or self.labels)
        return NERReport(
            model=self.name,
            labels_requested=active,
            hits=[NERHit(label="person", score=0.9, match_rate=1.0)],
        )

    def score_values(self, values, column_name, *, labels=None) -> NERResult:
        return self.analyze(values, column_name, labels=labels).to_result()

    def health_check(self) -> dict:
        return {"loadable": True}


def test_deep_scan_ner_writes_per_model_artifacts(tmp_path):
    df = pd.DataFrame({"name": ["Alice"], "city": ["Cairo"]})
    cfg = RedibisConfig()
    cfg.pii = PIIConfig(
        ner=NERConfig(
            models=[
                {"type": "gliner", "path": "/m1"},
                {"type": "gliner", "path": "/m2"},
            ],
            label_groups=[["person"], ["phone number"]],
        ),
    )
    m1 = _StubBackend("gliner:/m1")
    m2 = _StubBackend("gliner:/m2")

    with patch(
        "redibis.pii.ner_ensemble.NEREnsemble.from_config",
        return_value=__import__("redibis.pii.ner_ensemble", fromlist=["NEREnsemble"]).NEREnsemble([m1, m2]),
    ):
        artifact = _produce_ner_gliner(
            df=df,
            table="db.t",
            run_id="r1",
            span_id="s1",
            cap=type("Cap", (), {"id": "ner.gliner", "output": "pii_evidence", "engine": "gliner"})(),
            config=cfg,
            params={},
            run_dir=tmp_path,
        )

    evidence = tmp_path / "evidence"
    json_files = list(evidence.glob("ner.*.json"))
    assert len(json_files) == 2
    for p in json_files:
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "columns" in data
        assert "name" in data["columns"]
        text = p.read_text(encoding="utf-8")
        assert "Alice" not in text

    assert artifact.kind == "ner_agreement"
    assert artifact.summary.get("model_count") == 2


def test_normal_scan_uses_try_load_pii_not_ensemble(monkeypatch):
    from redibis.services import pipeline

    calls = {"try_load": 0, "ensemble": 0}

    def fake_try_load(**kw):
        calls["try_load"] += 1
        return _StubBackend("single")

    monkeypatch.setattr("redibis.pii.ner_registry.NERModelRegistry.try_load", fake_try_load)
    monkeypatch.setattr(
        "redibis.pii.detector.detect_pii",
        lambda *a, **kw: ([] if not kw.get("ner_backend") else [object()]),
    )

    df = pd.DataFrame({"a": ["x"]})
    from redibis.config import NERConfig
    pipeline.run_pii_detection(
        df,
        engines="both",
        ner_config=NERConfig(model_path="/m"),
    )
    assert calls["try_load"] >= 1
