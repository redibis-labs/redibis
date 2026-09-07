"""UI safety: Behavior settings are additive; default-off path unchanged."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_behavior_routes_registered_and_catalog_ok():
    from redibis.webapp.backend import app

    client = TestClient(app)
    r = client.get("/api/behavior/catalog")
    assert r.status_code == 200
    body = r.json()
    assert "catalogue" in body
    assert "manifest_sha256" in body


def test_behavior_settings_markers_in_app_js():
    """Behaviour tab is present but does not remove existing settings tabs."""
    root = Path(__file__).resolve().parents[1]
    js = (root / "redibis" / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'stab("behavior","Behavior")' in js
    assert 'stab("classification","Classification")' in js
    assert "function loadBehaviorPanel" in js
    # Existing frozen surfaces still present
    assert "function vSettings" in js
    assert 'stab("scan","Scan")' in js


def test_packs_settings_markers_in_app_js():
    """Packs tab is additive; frozen settings tabs remain."""
    root = Path(__file__).resolve().parents[1]
    js = (root / "redibis" / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'stab("packs","Packs")' in js
    assert 'stab("behavior","Behavior")' in js
    assert "function loadPacksPanel" in js
    assert "function exportDefaultPack" in js
    assert "function dryRunPackImport" in js
    assert "function confirmPackImport" in js
    assert "/api/rdbpack/stack" in js


def test_rdbpack_routes_registered():
    from redibis.webapp.backend import app

    client = TestClient(app)
    r = client.get("/api/rdbpack/stack")
    assert r.status_code == 200
    body = r.json()
    assert "layers" in body
    assert "count" in body


def test_behavior_config_defaults_disabled():
    from redibis.behavior.config import BehaviorConfig
    from redibis.behavior.models import RuntimeMode

    cfg = BehaviorConfig()
    assert cfg.enabled is False
    assert cfg.effective_mode("pii") is RuntimeMode.DISABLED
