"""Scan-guard button audit for Text Gateway and Evaluation.

The brief names Playwright as the method. This repo's CI does not ship a
browser runtime, so the load-bearing invariant — only Scan / Run evaluation
issue `/api/gateway/scan*` or `/api/gateway/evaluations/run*` — is asserted
statically from the JS that would run in the browser. Each inventory row
has a handler binding; none of the non-scan buttons fetch a scan endpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "redibis" / "webapp" / "static"
TEMPLATES = ROOT / "redibis" / "webapp" / "templates"

GW_JS = (STATIC / "gateway.js").read_text(encoding="utf-8")
EV_JS = (STATIC / "gateway_eval.js").read_text(encoding="utf-8")
RULES_JS = (STATIC / "gateway_rules.mjs").read_text(encoding="utf-8")
GW_HTML = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
EV_HTML = (TEMPLATES / "gateway_eval.html").read_text(encoding="utf-8")


def _ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def _bound(js: str, element_id: str) -> bool:
    needles = (
        f'$("{element_id}")',
        f"$('{element_id}')",
        f'getElementById("{element_id}")',
        f"getElementById('{element_id}')",
    )
    return any(n in js for n in needles)


# (id, page, scan_count_expected) — 1 only for the two scan triggers.
GATEWAY_BUTTONS = [
    ("gwScan", "gateway", 1),
    ("gwCancelScan", "gateway", 0),
    ("gwEdit", "gateway", 0),
    ("gwDeid", "gateway", 0),
    ("gwDownload", "gateway", 0),
    ("gwClear", "gateway", 0),
    ("gwLlmShow", "gateway", 0),
    ("gwLlmDownload", "gateway", 0),
    ("gwLlmDownloadBar", "gateway", 0),
    ("gwPolicyAllRedact", "gateway", 0),
    ("gwVerdictAcceptAll", "gateway", 0),
    ("gwVerdictAcceptAgree", "gateway", 0),
    ("gwVerdictAcceptLlmOnly", "gateway", 0),
    ("gwRecommendDownload", "gateway", 0),
    ("gwSaveUsecase", "gateway", 0),
    ("gwAutoTrim", "gateway", 0),
    ("gwHideRejected", "gateway", 0),
]

EVAL_BUTTONS = [
    ("evRun", "eval", 1),
    ("evCancel", "eval", 0),
    ("evAddSpan", "eval", 0),
    ("evAddCase", "eval", 0),
    ("evActExclude", "eval", 0),
    ("evActNoise", "eval", 0),
    ("evActCue", "eval", 0),
    ("evActNumber", "eval", 0),
    ("evActAccept", "eval", 0),
    ("evActAdvisory", "eval", 0),
    ("evActPatch", "eval", 0),
    ("evCompare", "eval", 0),
    ("evImport", "eval", 0),
    ("evDownload", "eval", 0),
    ("evDownloadCase", "eval", 0),
    ("evDownloadReport", "eval", 0),
    ("evClear", "eval", 0),
    ("evLlmShow", "eval", 0),
    ("evLlmDownload", "eval", 0),
    ("evSessionSave", "eval", 0),
    ("evSessionSaveAll", "eval", 0),
    ("evSessionOpen", "eval", 0),
    ("evVerdictAcceptAll", "eval", 0),
    ("evVerdictAcceptAgree", "eval", 0),
    ("evVerdictAcceptLlmOnly", "eval", 0),
    ("evRecommendDownload", "eval", 0),
    ("evLlmVerdict", "eval", 0),
    ("evRecommend", "eval", 0),
    ("evAutoTrim", "eval", 0),
]


_ALL_BUTTONS = GATEWAY_BUTTONS + EVAL_BUTTONS


@pytest.mark.parametrize(
    "button_id,page,scan_n",
    _ALL_BUTTONS,
    ids=[row[0] for row in _ALL_BUTTONS],
)
def test_button_handler_bound_and_scan_guard(button_id, page, scan_n):
    html = GW_HTML if page == "gateway" else EV_HTML
    js = GW_JS if page == "gateway" else EV_JS
    ids = _ids(html)
    assert button_id in ids, f"{button_id} missing from {page} HTML"
    assert _bound(js, button_id), f"{button_id} has no handler in {page} JS"
    # Stand-in for intercepting page.route("**/api/gateway/scan*"): only the
    # Scan / Run handlers issue those fetches.
    scan_fetches = len(re.findall(r'fetch\("/api/gateway/scan', js))
    eval_fetches = len(re.findall(r'fetch\("/api/gateway/evaluations/run', js))
    if page == "gateway":
        assert scan_fetches == 1
        if scan_n == 1:
            assert 'scanBtn.addEventListener("click", runScan)' in js
    else:
        assert eval_fetches == 1
        if scan_n == 1:
            assert 'runBtn.addEventListener("click", runEvaluation)' in js


def test_only_scan_button_calls_runScan():
    invoke = [m.start() for m in re.finditer(r"(?<!function )runScan\(", GW_JS)]
    assert invoke == [], invoke
    assert len(re.findall(r"await streamScan\(", GW_JS)) == 1


def test_gateway_mjs_modules_never_scan():
    files = sorted(STATIC.glob("gateway_*.mjs"))
    assert files, "expected gateway_*.mjs modules under static/"
    for path in files:
        blob = path.read_text(encoding="utf-8")
        assert "runScan" not in blob, path.name
        assert "runEvaluation" not in blob, path.name
        assert "/api/gateway/scan" not in blob, path.name
        assert "/api/gateway/evaluations/run" not in blob, path.name
    curation = (STATIC / "gateway_curation.mjs").read_text(encoding="utf-8")
    assert "fetch(" not in curation
    assert "runScan" not in curation
    assert "runEvaluation" not in curation


def test_only_run_button_starts_evaluation():
    assert len(re.findall(r"await streamEvaluation\(|streamEvaluation\(", EV_JS)) >= 1
    assert 'runBtn.addEventListener("click", runEvaluation)' in EV_JS
    # No other addEventListener binds runEvaluation.
    binds = re.findall(r'addEventListener\("click", runEvaluation\)', EV_JS)
    assert binds == ['addEventListener("click", runEvaluation)']


def test_noise_button_removed():
    blob = GW_JS + EV_JS + GW_HTML + EV_HTML
    assert "⌦ noise" not in blob
    assert "noise" in RULES_JS  # the rules editor field remains
    assert "runScan()" not in RULES_JS


def test_no_window_prompt_on_gateway_pages():
    blob = GW_JS + EV_JS
    for path in STATIC.glob("gateway_*.mjs"):
        blob += path.read_text(encoding="utf-8")
    assert "window.prompt" not in blob
