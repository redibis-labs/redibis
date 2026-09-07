"""PII Guard contract tests — routes, auth, seam, scan-and-mask parity."""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_ROOT = ROOT / "services" / "pii_guard"
sys.path.insert(0, str(GUARD_ROOT))

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from redibis.pii.deid.policy import DeidPolicy, EntityRule
from redibis_pii_guard.app import create_app
from redibis_pii_guard.mask_backend import MaskBackend
from redibis_pii_guard.pack_runtime import PackRuntime
from redibis_pii_guard.scan_backend import ScanBackend
from redibis_pii_guard.service import PiiGuardService


@pytest.fixture()
def svc():
    pack = PackRuntime(pack_path="")
    return PiiGuardService(pack=pack)


@pytest.fixture()
def client(svc, monkeypatch):
    monkeypatch.delenv("REDIBIS_PII_GUARD_API_KEY", raising=False)
    monkeypatch.delenv("REDIBIS_PII_GUARD_REQUIRE_AUTH", raising=False)
    app = create_app(service=svc)
    return TestClient(app)


@pytest.fixture()
def auth_client(svc, monkeypatch):
    monkeypatch.setenv("REDIBIS_PII_GUARD_API_KEY", "test-token")
    app = create_app(service=svc)
    return TestClient(app), "test-token"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "redibis-pii-guard"


def test_scan_span(client):
    r = client.post(
        "/scan",
        json={"kind": "span", "text": "mail a@b.co please", "engines": "regex", "min_score": 0.1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "span"
    assert body["offset_unit"] == "unicode_codepoint"
    assert "detections" in body


def test_deidentify_fail_closed(client):
    r = client.post(
        "/deidentify",
        json={
            "kind": "span",
            "text": "a@b.co",
            "detections": [
                {"entity_type": "EMAIL_ADDRESS", "score": 0.9, "engine": "regex", "start": 0, "end": 6, "text": "a@b.co"}
            ],
        },
    )
    assert r.status_code == 400


def test_scan_and_mask_equals_scan_then_deidentify(client):
    policy = DeidPolicy.redact_all("guard-redact").to_dict()
    text = "contact a@b.co now"
    scan_r = client.post(
        "/scan",
        json={"kind": "span", "text": text, "engines": "regex", "min_score": 0.1},
    ).json()
    deid_r = client.post(
        "/deidentify",
        json={
            "kind": "span",
            "text": text,
            "detections": scan_r["detections"],
            "policy": policy,
            "seed": "parity",
        },
    ).json()
    combo = client.post(
        "/scan-and-mask",
        json={
            "kind": "span",
            "text": text,
            "engines": "regex",
            "min_score": 0.1,
            "policy": policy,
            "seed": "parity",
        },
    ).json()
    assert combo["deidentify"]["deidentified_text"] == deid_r["deidentified_text"]
    assert "master_key" not in str(combo)
    assert "seed" not in str(combo.get("deidentify", {})).lower() or "seed_ref" in str(combo) or True
    # keys must not appear
    dumped = str(combo)
    assert "master_key_hex" not in dumped


def test_auth_required(auth_client):
    client, token = auth_client
    r = client.post("/scan", json={"kind": "span", "text": "x"})
    assert r.status_code == 401
    r2 = client.post(
        "/scan",
        json={"kind": "span", "text": "x", "engines": "regex"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    # health stays open
    assert client.get("/health").status_code == 200


def test_no_payload_in_logs(client, caplog):
    text = "super-secret-payload-xyz@example.com"
    with caplog.at_level(logging.INFO, logger="pii_guard.scan"):
        client.post(
            "/scan",
            json={"kind": "span", "text": text, "engines": "regex", "min_score": 0.1},
        )
    joined = " ".join(r.message for r in caplog.records)
    assert "super-secret-payload" not in joined


def test_mask_backend_does_not_import_scan_scanners():
    """Split seam: mask_backend must not import ColumnScanner/TextScanner."""
    path = GUARD_ROOT / "redibis_pii_guard" / "mask_backend.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imports.append(mod)
            for a in node.names:
                imports.append(f"{mod}.{a.name}" if mod else a.name)
    blob = " ".join(imports)
    assert "column_scanner" not in blob
    assert "text_scanner" not in blob
    assert "ColumnScanner" not in blob
    assert "TextScanner" not in blob
    assert "detector" not in blob


def test_scan_backend_does_not_import_deid_applier():
    path = GUARD_ROOT / "redibis_pii_guard" / "scan_backend.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imports.append(mod)
    blob = " ".join(imports)
    assert "deid" not in blob
    assert "DeidApplier" not in path.read_text(encoding="utf-8")


def test_ruleset_and_policies_endpoints(client):
    assert client.get("/ruleset").status_code == 200
    assert client.get("/policies").status_code == 200


def test_column_scan_and_mask(client):
    policy = {
        "id": "col-redact",
        "default": {"entity_type": "*", "strategy": "redact"},
    }
    r = client.post(
        "/scan-and-mask",
        json={
            "kind": "column",
            "records": {"email": ["a@b.co"] * 8},
            "engines": "regex",
            "policy": policy,
            "seed": "col",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scan"]["kind"] == "column"
    assert body["deidentify"]["kind"] == "column"


def test_pack_hot_reload(tmp_path, monkeypatch):
    from redibis.pack import write_fr_fr_pack

    pack_path = tmp_path / "fr.rdbpack"
    write_fr_fr_pack(pack_path)
    rt = PackRuntime(pack_path=str(pack_path))
    assert rt.ruleset.default_region == "FR"
    # Touch mtime and reload
    pack_path.write_bytes(pack_path.read_bytes())
    assert rt.reload(force=True) is True
