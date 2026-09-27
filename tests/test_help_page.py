"""/help — getting-started page rendered from static/help.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

HELP_JSON = Path(__file__).resolve().parents[1] / "redibis" / "webapp" / "static" / "help.json"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path / "configs"))
    import redibis.webapp.backend as web
    from redibis.webapp.store_accessors import clear_stores

    clear_stores()
    yield TestClient(web.app)
    clear_stores()


def test_help_page_renders_every_section(client):
    r = client.get("/help")
    assert r.status_code == 200
    data = json.loads(HELP_JSON.read_text(encoding="utf-8"))
    for sec in data["sections"]:
        assert f'id="{sec["id"]}"' in r.text
    assert "video coming soon" in r.text


def test_help_page_does_not_advertise_hidden_features(client):
    text = client.get("/help").text.lower()
    for word in ("commercial", "enterprise", "agent", "deep enrich", "evaluation"):
        assert word not in text, word


def test_help_video_links_only_allow_http(tmp_path):
    from redibis.webapp.help_content import load_help_content

    f = tmp_path / "help.json"
    f.write_text(json.dumps({"intro": "", "sections": [{"id": "a", "title": "A", "videos": [
        {"title": "ok", "url": "https://youtu.be/abc"},
        {"title": "bad", "url": "javascript:alert(1)"},
    ]}]}), encoding="utf-8")
    vids = load_help_content(f)["sections"][0]["videos"]
    assert vids[0]["url"] == "https://youtu.be/abc"
    assert vids[1]["url"] == ""


@pytest.mark.parametrize("path", ["/", "/settings", "/v2"])
def test_main_pages_link_to_help(client, path):
    assert 'href="/help"' in client.get(path).text
