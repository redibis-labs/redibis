"""PII detailed logging — multi-hit regex, decision channel, no value leak."""

from __future__ import annotations

import json
import logging
from io import StringIO
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.models import PIIDetection
from redibis.obs import DecisionRecord, decision, setup_logging
from redibis.obs.decision import set_decision_log_enabled
from redibis.pii.detector import _run_presidio, detect_pii


@pytest.fixture(autouse=True)
def _reset_decision_log():
    set_decision_log_enabled(True)
    yield
    set_decision_log_enabled(True)


class _FakeNERBackend:
    name = "fake:test"
    labels = ["person", "phone number"]

    def __init__(self, score=0.91, label="phone number"):
        self._score = score
        self._label = label
        self.calls: list[dict] = []

    def analyze(self, values, column_name, *, labels=None):
        from redibis.pii.ner_backend import NERHit, NERReport
        active = list(labels or self.labels)
        self.calls.append({"labels": active, "column": column_name})
        if not self._score:
            return NERReport(model=self.name, labels_requested=active)
        return NERReport(
            model=self.name,
            labels_requested=active,
            hits=[NERHit(label=self._label, score=self._score, match_rate=1.0)],
        )

    def score_values(self, values, column_name, *, labels=None):
        return self.analyze(values, column_name, labels=labels).to_result()

    def health_check(self):
        return {"loadable": True}


def test_run_presidio_regex_hits_include_regex_string():
    pytest.importorskip("presidio_analyzer")
    result = _run_presidio(
        ["+201001234567"],
        group="structured",
        arabic=False,
        column_name="phone",
    )
    assert result["regex_hits"]
    assert result["regex_hits"][0]["regex"]
    assert result["regex_hits"][0]["entity_type"]
    assert result["score"] is not None


def test_multi_entity_regex_hits_via_mock():
    df = pd.DataFrame({"mixed": ["+201001234567"]})
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    log = logging.getLogger("redibis.decision")
    log.handlers = [handler]
    log.setLevel(logging.INFO)

    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {
            "score": 0.9,
            "pattern": "test_phone",
            "match_rate": 1.0,
            "entity_type": "PHONE_NUMBER",
            "regex_hits": [
                {
                    "pattern_name": "test_phone",
                    "regex": r"\+201\d{9}",
                    "entity_type": "PHONE_NUMBER",
                    "score": 0.9,
                    "match_rate": 1.0,
                    "group": "structured",
                    "collision_group": None,
                    "validator": None,
                },
                {
                    "pattern_name": "test_email",
                    "regex": r"[\w]+@example\.com",
                    "entity_type": "EMAIL_ADDRESS",
                    "score": 0.85,
                    "match_rate": 0.5,
                    "group": "structured",
                    "collision_group": None,
                    "validator": None,
                },
            ],
        }
        detections = detect_pii(
            df,
            engines="regex",
            table="db.t",
            log_regex_hits=True,
        )

    assert len(detections) == 1
    assert len(detections[0].regex_hits) == 2
    assert detections[0].regex_hits[0]["regex"]
    out = buf.getvalue()
    assert "DECISION" in out
    assert "PHONE_NUMBER" in out
    assert "EMAIL_ADDRESS" in out


def test_no_sample_values_in_log_when_log_samples_false():
    secret = "super-secret-pii-value-12345"
    df = pd.DataFrame({"email": [secret]})
    messages: list[str] = []

    def capture(msg):
        messages.append(msg)

    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {
            "score": 0.9,
            "pattern": "email",
            "match_rate": 1.0,
            "entity_type": "EMAIL_ADDRESS",
            "regex_hits": [],
        }
        detect_pii(
            df,
            engines="regex",
            progress_callback=capture,
            log_samples=False,
        )

    joined = "\n".join(messages)
    assert secret not in joined
    assert "n=1" in joined or "avg_len" in joined


def test_ner_label_groups_multi_pass():
    df = pd.DataFrame({"x": ["Alice"]})
    backend = _FakeNERBackend()
    from redibis.pii.ner_ensemble import NEREnsemble
    ensemble = NEREnsemble([backend])
    with patch("redibis.pii.detector._run_presidio") as mock_presidio:
        mock_presidio.return_value = {
            "score": None,
            "pattern": None,
            "match_rate": 0.0,
            "entity_type": None,
            "regex_hits": [],
        }
        detections = detect_pii(
            df,
            engines="both",
            ensemble=ensemble,
            gliner_always_run=True,
            ner_label_groups=[["person"], ["phone number"]],
            table="db.t",
        )

    assert len(backend.calls) == 2
    assert len(detections[0].ner_hits) == 2
    assert detections[0].ner_hits[0]["labels"] == ["person"]
    assert detections[0].ner_hits[1]["labels"] == ["phone number"]
