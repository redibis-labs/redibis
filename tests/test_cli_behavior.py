"""CLI coverage for Behavior Policy lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.cli.main import main


MINIMAL_DOC = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {
        "id": "cli-lac-policy",
        "version": "1.0.0",
        "description": "Demote LAC via CLI",
    },
    "appliesTo": {"engine": "pii", "stage": "post_verdict"},
    "rules": [
        {
            "id": "lac-network",
            "when": {"fact": "column.name", "op": "matches", "value": "(?i)^lac$"},
            "effects": [
                {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}},
                {"effect": "core.verdict.set_detected", "params": {"detected": True}},
            ],
            "reason": "LAC is a network identifier, not a phone number.",
            "priority": 10,
            "terminal": True,
        }
    ],
}


def _run(argv):
    return main(argv)


def _last_json(capsys) -> dict:
    """Parse the last JSON object from captured stdout (ignore prior commands)."""
    out = capsys.readouterr().out.strip()
    # Prefer the last top-level JSON object if multiple were printed
    decoder = json.JSONDecoder()
    idx = 0
    last = None
    while idx < len(out):
        while idx < len(out) and out[idx].isspace():
            idx += 1
        if idx >= len(out):
            break
        # skip non-json noise (log lines)
        if out[idx] not in "{[":
            nl = out.find("\n", idx)
            if nl < 0:
                break
            idx = nl + 1
            continue
        obj, end = decoder.raw_decode(out, idx)
        last = obj
        idx = end
    if last is None:
        raise AssertionError(f"no JSON found in CLI output:\n{out[:500]}")
    return last


@pytest.fixture
def store_dir(tmp_path):
    d = tmp_path / "behavior-policies"
    d.mkdir()
    return d


def test_behavior_catalog_and_status(store_dir, capsys):
    assert _run(["behavior", "catalog", "--json", "--store-dir", str(store_dir)]) == 0
    out = _last_json(capsys)
    assert "catalogue" in out
    assert "core.verdict.set_entity" in out["catalogue"]["effects"]

    assert _run(["behavior", "status", "--json", "--store-dir", str(store_dir)]) == 0
    status = _last_json(capsys)
    assert status["enabled"] is False
    assert status["effective_mode_pii"] == "disabled"


def test_behavior_lifecycle_cli(store_dir, tmp_path, capsys):
    doc = tmp_path / "policy.json"
    doc.write_text(json.dumps(MINIMAL_DOC), encoding="utf-8")
    sd = str(store_dir)

    assert _run(["behavior", "validate", str(doc), "--store-dir", sd, "--json"]) == 0
    capsys.readouterr()  # discard validate output

    assert _run([
        "behavior", "create", str(doc), "--store-dir", sd, "--actor", "tester", "--json",
    ]) == 0
    created = _last_json(capsys)
    assert created["status"] == "draft"

    assert _run([
        "behavior", "simulate",
        "--policy-id", created["id"],
        "--version", created["version"],
        "--column", "lac",
        "--facts", json.dumps({
            "column.name": "lac",
            "verdict.entity": "PHONE_NUMBER",
            "verdict.detected": True,
        }),
        "--store-dir", sd,
        "--json",
    ]) == 0
    sim = _last_json(capsys)
    assert sim["after"].get("entity") == "NETWORK_ID"

    assert _run([
        "behavior", "approve", created["id"], created["version"],
        "--actor", "steward", "--simulation-id", sim["simulation_id"],
        "--store-dir", sd, "--json",
    ]) == 0
    capsys.readouterr()

    assert _run([
        "behavior", "activate", created["id"], created["version"],
        "--actor", "admin", "--store-dir", sd, "--json",
    ]) == 0
    act = _last_json(capsys)
    assert act["status"] == "active"

    assert _run(["behavior", "list", "--store-dir", sd, "--json"]) == 0
    listed = _last_json(capsys)
    assert any(p["active"] for p in listed["policies"])

    assert _run([
        "behavior", "promote",
        "--column", "cell_lac",
        "--from-entity", "PHONE_NUMBER",
        "--to-entity", "NETWORK_ID",
        "--table", "telecom.cells",
        "--store-dir", sd,
        "--json",
    ]) == 0
    promo = _last_json(capsys)
    assert promo["status"] == "draft"

    assert _run(["behavior", "metrics", "--store-dir", sd, "--json"]) == 0
    metrics = _last_json(capsys)
    assert metrics["policies_active"] >= 1

    assert _run([
        "behavior", "outcomes", "record",
        "--policy-id", created["id"],
        "--rule-id", "lac-network",
        "--policy-sha", created["content_sha256"],
        "--original", "PHONE_NUMBER",
        "--policy-verdict", "NETWORK_ID",
        "--human-result", "confirmed",
        "--context-hash", "abc123",
        "--table", "telecom.cells",
        "--column", "lac",
        "--store-dir", sd,
        "--json",
    ]) == 0
    capsys.readouterr()

    assert _run(["behavior", "signals", "--store-dir", sd, "--json"]) == 0
    signals = _last_json(capsys)
    assert signals["signals"]

    assert _run(["behavior", "plugins", "--store-dir", sd, "--json"]) == 0
    plugs = _last_json(capsys)
    assert "allowlist" in plugs
