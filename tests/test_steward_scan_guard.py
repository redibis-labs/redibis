"""Static scan-guard: steward review never fetches a scan endpoint."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MJS = (ROOT / "redibis" / "webapp" / "static" / "steward_review.mjs").read_text(encoding="utf-8")
WS = (ROOT / "redibis" / "webapp" / "static" / "contract_workspace.mjs").read_text(encoding="utf-8")
V2 = (ROOT / "redibis" / "webapp" / "templates" / "v2.html").read_text(encoding="utf-8")


def test_steward_tab_renamed():
    assert "Steward Review" in V2
    assert "renderStewardReview" in V2


def test_steward_module_has_no_scan_fetch():
    scans = re.findall(r"/api/(?:scan|gateway/scan|gateway/evaluations/run)[^\"'\s]*", MJS)
    assert scans == []
    assert "mountStewardReview" in MJS
    for key in ("accept", "reject", "needs_review", "no_action", "edit"):
        assert key in MJS
    assert "no_evidence" in MJS
    assert "no engine has scanned this field" in MJS
    assert "PII on" in MJS
    assert "PII off" in MJS
    assert "Save edits" in MJS
    assert "/verdicts" in MJS
    assert "Export verdicts" in MJS
    assert "Export all artifacts" in MJS
    assert "Write your own" in MJS
    assert "steward/export/verdicts" in MJS
    assert "sr-pii-icon" in MJS
    assert "column-field" in MJS
    assert "steward-header.ok" in MJS
    assert "&#8203;" in WS
    assert "width:16px !important" in V2
    assert "overflow-wrap:break-word" in V2
    assert "steward-header" in MJS
    assert "srTableSave" in MJS
    assert "steward/export/artifacts" in MJS
    assert "steward artifacts (zip)" in V2
    assert "exportStewardArtifacts" in V2


def test_workspace_module_has_no_scan_fetch():
    scans = re.findall(r"/api/(?:scan|gateway/scan|gateway/evaluations/run)[^\"'\s]*", WS)
    assert scans == []
    assert "mountContractWorkspace" in WS
    assert "mountContractWorkspace" in V2
    assert "?ws=" in V2 or "X-Redibis-Workspace" in V2


def test_v2_contract_and_synthesis_fetches_carry_workspace():
    """Raw fetch() to contract/synthesis APIs must set X-Redibis-Workspace."""
    assert 'opt.headers["X-Redibis-Workspace"] = STATE.ws' in V2
    fetches = [m.start() for m in re.finditer(r"\bfetch\s*\(", V2)]
    assert fetches, "expected fetch() in v2.html"
    for idx in fetches:
        window = V2[max(0, idx - 500): idx + 250]
        hits_contract = "/api/contracts" in window or "/api/synthesis" in window
        if not hits_contract:
            continue
        through_api = "async function api(" in V2[max(0, idx - 800): idx]
        sets_header = "X-Redibis-Workspace" in window
        assert through_api or sets_header, window[-400:]
