"""Tests for agent run defaults pack."""

from __future__ import annotations

import pytest

from redibis.agents.run_defaults import load_defaults, merge_defaults_into_params, save_defaults_text


def test_load_defaults_builtin():
    d = load_defaults("defaults")
    assert d["name"] == "defaults"
    assert d["pii"]["engines"] == "both"
    assert d["quality"]["sample_rows"] == 10000


def test_merge_defaults_into_params():
    merged = merge_defaults_into_params({})
    assert merged["pii_engines"] == "both"
    assert merged["quality_sample_rows"] == 10000
    merged2 = merge_defaults_into_params({"pii_engines": "regex"})
    assert merged2["pii_engines"] == "regex"


def test_save_defaults_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_AGENT_DEFAULTS_DIR", str(tmp_path))
    from redibis.agents.run_defaults import get_defaults_text
    raw = get_defaults_text("defaults")
    save_defaults_text(raw.replace("both", "regex"), "defaults")
    d = load_defaults("defaults")
    assert d["pii"]["engines"] == "regex"
