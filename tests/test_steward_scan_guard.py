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


def test_workspace_module_has_no_scan_fetch():
    scans = re.findall(r"/api/(?:scan|gateway/scan|gateway/evaluations/run)[^\"'\s]*", WS)
    assert scans == []
    assert "mountContractWorkspace" in WS
    assert "mountContractWorkspace" in V2
    assert "?ws=" in V2 or "X-Redibis-Workspace" in V2
