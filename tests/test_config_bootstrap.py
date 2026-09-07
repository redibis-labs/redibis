"""Bundled default named-config seeding."""

from __future__ import annotations

import json

import pytest

from redibis.store.config_store import LocalConfigStore
from redibis.store.config_bootstrap import ensure_bundled_configs


def test_ensure_bundled_configs_seeds_defaults(tmp_path):
    store = LocalConfigStore(tmp_path)
    seeded = ensure_bundled_configs(store)
    assert "redibis-default" in seeded["regex"]
    assert "merchant-curated-demo" in seeded["quality"]

    names = {c["name"] for c in store.list_regex_configs()}
    assert "redibis-default" in names
    redibis = next(c for c in store.list_regex_configs() if c["name"] == "redibis-default")
    assert redibis["uses_builtin_catalog"] is True
    assert redibis["pattern_count"] > 0

    second = ensure_bundled_configs(store)
    assert second["regex"] == []
    assert second["quality"] == []


def test_configs_export_includes_regex_default_and_catalog(client, tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))

    from redibis.webapp import store_accessors

    store_accessors.clear_stores()

    r = client.get("/api/configs/export?include_catalog=true")
    assert r.status_code == 200
    body = r.json()
    assert "redibis-default" in body["regex"]
    rd = body["regex"]["redibis-default"]
    assert rd["uses_builtin_catalog"] is True
    assert rd["effective_pattern_count"] > 0
    assert len(rd["effective_patterns"]) == rd["effective_pattern_count"]
    assert body["defaults"]["regex_config"] == "redibis-default"
    assert body["builtin_regex_catalog"]
    assert len(body["builtin_regex_catalog"]) > 0
    assert "merchant-curated-demo" in body["quality"]


def test_seed_defaults_endpoint(client, tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "cfg2"))

    from redibis.webapp import store_accessors

    store_accessors.clear_stores()

    r = client.post("/api/configs/seed-defaults")
    assert r.status_code == 200

    rx = client.get("/api/configs/regex").json()
    assert any(c["name"] == "redibis-default" for c in rx)

    r2 = client.post("/api/configs/seed-defaults")
    assert r2.json()["seeded"]["regex"] == []
    assert r2.json()["seeded"]["quality"] == []


@pytest.fixture
def client():
    from redibis.webapp.backend import app
    from fastapi.testclient import TestClient

    return TestClient(app)
