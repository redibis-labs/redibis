"""Detector integration with NEREnsemble."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

import pandas as pd

from redibis.pii.detector import detect_pii
from redibis.pii.ner_backend import NERHit, NERReport, NERResult
from redibis.pii.ner_ensemble import NEREnsemble


@dataclass
class _StubBackend:
    name: str
    labels: list[str] = field(default_factory=lambda: ["person"])
    score: float = 0.85
    label: str = "person"

    def analyze(self, values, column_name, *, labels=None):
        active = list(labels or self.labels)
        if not self.score:
            return NERReport(model=self.name, labels_requested=active)
        return NERReport(
            model=self.name,
            labels_requested=active,
            hits=[NERHit(label=self.label, score=self.score, match_rate=1.0)],
        )

    def score_values(self, values, column_name, *, labels=None) -> NERResult:
        return self.analyze(values, column_name, labels=labels).to_result()

    def health_check(self) -> dict:
        return {"loadable": True}


def test_single_backend_regression():
    df = pd.DataFrame({"name": ["Alice"]})
    backend = _StubBackend("fake:test", score=0.91, label="person")
    with patch("redibis.pii.detector._run_presidio") as mock_rx:
        mock_rx.return_value = {
            "score": None, "pattern": None, "match_rate": 0.0,
            "entity_type": None, "regex_hits": [],
        }
        detections = detect_pii(df, engines="both", ner_backend=backend, gliner_always_run=True)
    assert detections[0].gliner_score == 0.91
    assert detections[0].gliner_label == "person"
    assert len(detections[0].ner_hits) == 1


def test_ensemble_multi_model_multi_group():
    df = pd.DataFrame({"x": ["val"]})
    m1 = _StubBackend("m1", label="person", score=0.8)
    m2 = _StubBackend("m2", label="phone number", score=0.75)
    ensemble = NEREnsemble([m1, m2])
    with patch("redibis.pii.detector._run_presidio") as mock_rx:
        mock_rx.return_value = {
            "score": None, "pattern": None, "match_rate": 0.0,
            "entity_type": None, "regex_hits": [],
        }
        detections = detect_pii(
            df,
            engines="both",
            ensemble=ensemble,
            ner_label_groups=[["person"], ["phone number"]],
            gliner_always_run=True,
        )
    hits = detections[0].ner_hits
    assert len(hits) == 4
    models = {h["model"] for h in hits}
    assert models == {"m1", "m2"}
    assert all("model_slug" in h for h in hits)
    assert detections[0].gliner_score == 0.8


def test_ensemble_verdict_uses_primary_model_only():
    """Secondary model must not inflate the fusion score (decision #3)."""
    df = pd.DataFrame({"x": ["val"]})
    m1 = _StubBackend("m1", label="person", score=0.5)
    m2 = _StubBackend("m2", label="phone number", score=0.99)
    ensemble = NEREnsemble([m1, m2])
    with patch("redibis.pii.detector._run_presidio") as mock_rx:
        mock_rx.return_value = {
            "score": None, "pattern": None, "match_rate": 0.0,
            "entity_type": None, "regex_hits": [],
        }
        detections = detect_pii(
            df,
            engines="both",
            ensemble=ensemble,
            gliner_always_run=True,
        )
    assert detections[0].gliner_score == 0.5
    assert detections[0].gliner_label == "person"
    assert len(detections[0].ner_hits) == 2
