"""UI safety markers for the free-text PII playground (additive only)."""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "redibis" / "webapp" / "static" / "app.js"


def test_playground_view_is_additive():
    src = APP_JS.read_text(encoding="utf-8")
    assert 'pii-playground' in src
    assert "open-pii-playground" in src
    assert "highlightPiiSpans" in src
    assert "Array.from" in src  # code-point walker
    assert "/api/pii/text/scan" in src
    assert "/api/pii/text/deidentify" in src
    assert "pii-play-save-policy" in src
    assert "suggest-policy" in src
    assert "Policy panel" in src
    assert "reversible with key" in src
    # Frozen discovery flow still present
    assert "pii-discover" in src
    assert "open-pii-discover" in src


def test_highlight_escapes_markup_contract():
    """Client splices with E() — never injects raw server HTML."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "function highlightPiiSpans" in src
    # Escaped slices before wrapping in <mark>
    assert "E(cps.slice" in src
