"""The PII capability contract: every example holds on the engine, and the document says so."""

from __future__ import annotations

import json
import logging

import pandas as pd
import pytest

from redibis.pii import capability as cap
from redibis.pii.capability.render import find_chromium

CONTRACT = cap.load_contract()
CASES = [case for _section, case in cap.iter_cases(CONTRACT)]


@pytest.fixture(scope="module")
def verification():
    logger = logging.getLogger("redibis")
    level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        return cap.verify(CONTRACT)
    finally:
        logger.setLevel(level)


# ── the contract file ────────────────────────────────────────────────────────

def test_contract_shape_and_unique_ids():
    assert CONTRACT["kind"] == cap.CONTRACT_KIND and CONTRACT["version"]
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in CASES:
        assert case["kind"] in ("column", "text") and case["title"] and "expect" in case, case["id"]
        if case["kind"] == "column":
            assert case["column"] and len(case["values"]) >= 3, case["id"]
        else:
            assert case["text"] and (case["expect"].get("spans") or case["expect"].get("absent")), case["id"]
    for section in CONTRACT["entities"]:
        assert section["description"] and section["category"] and section["cases"], section["entity_type"]
    assert CONTRACT["change_process"]["request_template"]["must_detect"]


def test_every_catalogue_type_is_in_the_contract():
    """A new PII type in the regex catalogue must come with contract examples."""
    from redibis.pii.regex_catalog import list_catalog

    aliases = {"JWT_TOKEN": "JWT"}
    catalogued = {aliases.get(e["entity_type"], e["entity_type"]) for e in list_catalog()}
    documented = {s["entity_type"] for s in CONTRACT["entities"]}
    assert catalogued <= documented, f"add contract sections for: {sorted(catalogued - documented)}"


def test_every_documented_type_has_a_detected_example():
    for section in CONTRACT["entities"]:
        positive = [c for c in section["cases"]
                    if (c["kind"] == "column" and c["expect"].get("detected")) or
                    (c["kind"] == "text" and c["expect"].get("spans"))]
        assert positive, f"{section['entity_type']} has no example that must be detected"


def test_limits_are_documented_with_examples():
    limits = [c for s in CONTRACT["not_supported"] for c in s["cases"]]
    assert len(limits) >= 8
    assert all((c["kind"] == "column" and not c["expect"]["detected"]) or
               (c["kind"] == "text" and c["expect"]["absent"]) for c in limits)


# ── every example on the engine ──────────────────────────────────────────────

@pytest.mark.parametrize("case_id", [c["id"] for c in CASES])
def test_example_holds_on_the_engine(verification, case_id):
    result = next(r for r in verification["results"] if r["id"] == case_id)
    if result["status"] == "not_verified":
        pytest.skip("; ".join(result["problems"]))
    assert result["status"] == "passed", "; ".join(result["problems"])


def test_verification_summary(verification):
    s = verification["summary"]
    assert s["total"] == len(CASES) and s["failed"] == 0
    assert s["passed"] + s["not_verified"] == s["total"]
    assert verification["environment"]["engines"]["regex"] is True


def test_a_wrong_expectation_is_reported_not_hidden():
    case = {"id": "t", "kind": "column", "title": "t", "column": "email",
            "values": ["a@x.com", "b@y.org", "c@z.net"], "expect": {"detected": True, "entity_type": "IBAN_CODE"}}
    result = cap.verify_case(case)
    assert result["status"] == "failed" and "expected IBAN_CODE" in result["problems"][0]
    text = {"id": "t2", "kind": "text", "title": "t", "text": "call 01012345678",
            "expect": {"spans": [], "absent": [{"value": "01012345678"}]}}
    assert cap.verify_case(text)["status"] == "failed"
    needs = dict(case, requires=["llm"])
    assert cap.verify_case(needs, {**cap.environment(), "engines": {"llm": False}})["status"] == "not_verified"


# ── the document ─────────────────────────────────────────────────────────────

def test_document_lists_every_example_with_its_status(verification):
    html = cap.render_html(CONTRACT, verification)
    for case in CASES:
        assert case["id"] in html
    assert f"{verification['summary']['passed']}</b>verified" in html.replace("\n", "")
    assert "Request template" in html and "edge rule" in html
    unverified = cap.render_html(CONTRACT, None)
    assert "Not verified" in unverified


def test_tuning_guide_renders():
    md = cap.guide_markdown()
    assert md.startswith("# Adding and tuning PII detection") and "_Last updated:" in md
    assert "<h1>" in cap.render_guide_html()


@pytest.mark.skipif(find_chromium() is None, reason="PDF needs Chromium")
def test_pdf(verification):
    pdf = cap.render_pdf(cap.render_html(CONTRACT, verification))
    assert pdf.startswith(b"%PDF-") and len(pdf) > 50_000


# ── CLI and API ──────────────────────────────────────────────────────────────

def test_cli_verify_report_export_guide(tmp_path, capsys):
    from redibis.cli.main import main as cli

    assert cli(["pii", "capabilities", "verify", "--only", "email-col,phone-text-en"]) == 0
    assert "2/2 verified" in capsys.readouterr().err
    out = tmp_path / "contract.html"
    assert cli(["pii", "capabilities", "report", "--no-verify", "-o", str(out)]) == 0
    assert "PII detection capability contract" in out.read_text(encoding="utf-8")
    exported = tmp_path / "contract.json"
    assert cli(["pii", "capabilities", "export", "-o", str(exported)]) == 0
    assert json.loads(exported.read_text(encoding="utf-8"))["kind"] == cap.CONTRACT_KIND
    assert cli(["pii", "capabilities", "guide", "--format", "md", "-o", str(tmp_path / "g.md")]) == 0

    broken = json.loads(json.dumps(CONTRACT))
    broken["entities"][0]["cases"][0]["expect"]["entity_type"] = "IBAN_CODE"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(broken), encoding="utf-8")
    first = broken["entities"][0]["cases"][0]["id"]
    assert cli(["pii", "capabilities", "verify", "--contract", str(bad), "--only", first]) == 1


def test_api_routes(monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend, pii_capability_routes

    monkeypatch.setattr(pii_capability_routes, "_LAST", {})
    client = TestClient(backend.app)
    assert client.get("/api/pii/capabilities").json()["kind"] == cap.CONTRACT_KIND
    assert client.get("/api/pii/capabilities/verification").status_code == 404
    monkeypatch.setattr(cap, "verify", lambda contract=None, **k: {
        "summary": {"total": 1, "passed": 1, "failed": 0, "not_verified": 0}, "results": [],
        "environment": {}, "verified_at": "2026-09-27T00:00:00+00:00", "seconds": 0.1})
    assert client.post("/api/pii/capabilities/verify").json()["summary"]["passed"] == 1
    assert client.get("/api/pii/capabilities/verification").status_code == 200
    r = client.get("/api/pii/capabilities/report?format=html&verify=last&download=true")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "capability contract" in r.text
    assert "<h1>" in client.get("/api/pii/capabilities/guide").text
    assert client.get("/api/pii/capabilities/guide?format=md").text.startswith("# Adding")
    assert client.get("/api/pii/capabilities/report?format=doc").status_code == 422


# ── engine fixes found while writing the contract ────────────────────────────

def _column(name, values):
    from redibis.services.pipeline import run_pii_detection

    [det] = run_pii_detection(pd.DataFrame({name: values}), engines="regex", policy_pack="telecom")
    return det.detected, det.entity_type


def test_validators_that_read_letters_get_the_whole_value():
    """IBAN, MAC and wallet validators used to receive digits only and always failed."""
    from redibis.pii.telecom_signals import apply_sim_gates

    hit = {"pattern_name": "iban_egypt", "entity_type": "IBAN_CODE", "validator": "validate_iban", "score": 1.0}
    ibans = [c for s in CONTRACT["entities"] if s["entity_type"] == "IBAN_CODE"
             for c in s["cases"] if c["id"] == "iban-col-eg"][0]["values"]
    assert apply_sim_gates([dict(hit)], ibans, "iban")
    assert _column("device_mac", ["00:1A:2B:3C:4D:5E", "3C:5A:B4:12:9F:01", "F0:18:98:AA:BB:CC"]) == (
        True, "MAC_ADDRESS")


def test_same_entity_formats_are_not_suppressed_by_the_column_name():
    """The column name picks the entity; every format of that entity keeps running."""
    mc = [c for s in CONTRACT["entities"] if s["entity_type"] == "CREDIT_CARD"
          for c in s["cases"] if c["id"] == "card-col-mc"][0]["values"]
    assert _column("card_number", mc) == (True, "CREDIT_CARD")
    assert _column("tax_id", ["123-456-789", "234-567-890", "345-678-901"]) == (True, "EG_TAX_ID")
    # a different entity in the same collision group is still suppressed
    assert _column("tax_id", ["123456789", "234567890", "345678901"]) == (True, "EG_TAX_ID")
