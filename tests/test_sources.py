"""Tests for SessionSource registry."""

from __future__ import annotations

import os

import pytest

from redibis.services.session.sources import (
    SessionSource,
    get_source,
    list_sources,
    register_source,
)


def test_list_sources_builtin():
    names = {s.name for s in list_sources()}
    assert "scan" in names
    assert "agent" in names


def test_get_source_scan_env_override(tmp_path, monkeypatch):
    root = tmp_path / "custom_scan"
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(root))
    src = get_source("scan")
    assert src.root() == root.resolve()


def test_get_source_unknown_raises():
    with pytest.raises(KeyError):
        get_source("nonexistent")


def test_register_source(tmp_path):
    custom = SessionSource("custom", "Custom", "CUSTOM_OUTPUT_DIR", str(tmp_path / "fallback"))
    register_source(custom)
    assert get_source("custom").label == "Custom"
    assert get_source("custom").root() == (tmp_path / "fallback").resolve()
