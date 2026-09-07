"""Presidio regex path parity — no spaCy NLP engine required."""

from __future__ import annotations

import pytest

pytest.importorskip("presidio_analyzer")


def test_run_presidio_detects_structured_phone():
    """Pattern recognizers fire with the no-op NLP engine (no spaCy boot)."""
    from redibis.pii.detector import _run_presidio

    result = _run_presidio(
        ["+201001234567"],
        group="structured",
        arabic=False,
        column_name="phone",
    )
    assert result["score"] is not None
    assert result["score"] >= 0.5
    assert result["entity_type"] is not None


def test_run_presidio_merchant_golden_email_sample():
    """Golden-style email column value matches catalog regex."""
    from redibis.pii.detector import _run_presidio

    result = _run_presidio(
        ["merchant.contact@example.com"],
        group="structured",
        arabic=False,
        column_name="contact_email",
    )
    assert result["score"] is not None
    assert result["match_rate"] == 1.0


def test_analyzer_engine_with_noop_nlp_engine():
    """Direct Presidio smoke — pattern-only registry, explicit no-op engine."""
    from presidio_analyzer import RecognizerRegistry
    from presidio_analyzer.pattern import Pattern
    from presidio_analyzer.pattern_recognizer import PatternRecognizer

    from redibis.pii.presidio_nlp import build_pattern_analyzer_engine

    registry = RecognizerRegistry()
    registry.add_recognizer(
        PatternRecognizer(
            supported_entity="PHONE",
            patterns=[Pattern(name="digits", regex=r"\d{10,}", score=0.8)],
        )
    )
    engine = build_pattern_analyzer_engine(registry, supported_languages=["en"])
    hits = engine.analyze("1234567890123", language="en")
    assert len(hits) == 1
    assert hits[0].entity_type == "PHONE"
