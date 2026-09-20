"""Curation persist/restore and scan-guard (Playwright stand-in).

The brief names a browser flow: scan once, reject/modify, assert zero further
scan requests, reload restores the record. This repo's CI has no Playwright
runtime, so the load-bearing pieces are asserted as (a) the JS never fetches
``/api/gateway/scan*`` from curation controls, and (b) the API round-trips a
curation record keyed by ``run_uuid`` with no surface text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "redibis" / "webapp" / "static"
GW_JS = (STATIC / "gateway.js").read_text(encoding="utf-8")
EV_JS = (STATIC / "gateway_eval.js").read_text(encoding="utf-8")
CUR_JS = (STATIC / "gateway_curation.mjs").read_text(encoding="utf-8")


def test_curation_controls_exist_and_do_not_scan():
    for label in ("✓ keep", "✗ reject", "✎ edit", "⟲ trim", "→ make a rule"):
        assert label in GW_JS
    assert "⌦ noise" not in GW_JS
    assert "runScan()" not in CUR_JS
    start = GW_JS.find("async function decide(")
    assert start != -1
    nxt = GW_JS.find("\nasync function ", start + 1)
    blob = GW_JS[start:nxt if nxt != -1 else start + 800]
    assert "/api/gateway/scan" not in blob
    assert "upsertEntry" in blob
    assert len(re.findall(r'fetch\("/api/gateway/scan', GW_JS)) == 1


def test_eval_predicted_reject_does_not_scan():
    assert "curatePredicted" in EV_JS
    assert "✗ reject" in EV_JS
    assert "✓ keep" in EV_JS
    assert len(re.findall(r'fetch\("/api/gateway/evaluations/run', EV_JS)) == 1
    start = EV_JS.find("async function curatePredicted(")
    assert start != -1
    nxt = EV_JS.find("\nfunction ", start + 1)
    blob = EV_JS[start:nxt if nxt != -1 else start + 1200]
    assert "/api/gateway/scan" not in blob
    assert "/api/gateway/evaluations/run" not in blob


def test_download_json_includes_curation_and_verdict():
    start = GW_JS.find("function downloadJson(")
    nxt = GW_JS.find("\nasync function ", start + 1)
    blob = GW_JS[start:nxt if nxt != -1 else start + 900]
    assert "payload.curation" in blob
    assert "llm_verdict" in blob
    assert "curatedSpans()" in blob
    assert "rejected_spans" in blob


def test_scan_reloads_saved_curation():
    assert 'fetch("/api/gateway/curation/"' in GW_JS
    assert 'method: "POST"' in GW_JS
    assert "/api/gateway/curation" in GW_JS


@pytest.fixture
def client(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    from redibis.webapp.backend import app

    with TestClient(app) as c:
        yield c


_CURATION = {
    "kind": "redibis.span_curation",
    "schema_version": "1.0",
    "run_uuid": "run-abc123",
    "text_digest": "deadbeef",
    "auto_trim": False,
    "entries": [
        {
            "key": {"start": 0, "end": 5, "entity_type": "PERSON", "source": "engine"},
            "decision": "reject",
        },
        {
            "key": {"start": 10, "end": 13, "entity_type": "PERSON", "source": "engine"},
            "decision": "reject",
        },
        {
            "key": {"start": 20, "end": 30, "entity_type": "PERSON", "source": "engine"},
            "decision": "modify",
            "new_start": 20,
            "new_end": 25,
        },
    ],
}


def test_curation_api_roundtrip_has_no_surfaces(client, tmp_path):
    r = client.post("/api/gateway/curation", json={"run_uuid": "run-abc123", "curation": _CURATION})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "saved"
    assert len(body["curation"]["entries"]) == 3
    blob = json.dumps(body["curation"])
    assert '"text"' not in blob
    assert '"surface"' not in blob
    stored = tmp_path / "pii_curation" / "run-abc123.json"
    assert stored.is_file()
    got = client.get("/api/gateway/curation/run-abc123")
    assert got.status_code == 200, got.text
    restored = got.json()
    assert restored["run_uuid"] == "run-abc123"
    assert [e["decision"] for e in restored["entries"]] == ["reject", "reject", "modify"]
    assert restored["entries"][2]["new_start"] == 20
    assert restored["entries"][2]["new_end"] == 25
    assert "text" not in json.dumps(restored["entries"])


def test_curation_api_refuses_surface_text(client):
    bad = {
        "run_uuid": "run-bad",
        "entries": [
            {
                "key": {"start": 0, "end": 5, "entity_type": "PERSON", "source": "engine"},
                "decision": "reject",
                "text": "Alice",
            }
        ],
    }
    r = client.post("/api/gateway/curation", json={"run_uuid": "run-bad", "curation": bad})
    assert r.status_code == 400, r.text
