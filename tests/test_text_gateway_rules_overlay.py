"""Text Gateway rule overlay — exclusions, patterns, quantities."""

from __future__ import annotations

from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.rules.text_filters import (
    NumberContextClassifier,
    TermExclusionFilter,
    apply_text_rule_filters,
)
from redibis.pii.scan.result import Candidate, TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner
from redibis.pii.text_rules import TextRuleOverlay, compile_text_rules, default_text_rules


def test_default_overlay_excludes_agent_and_has_address_cue():
    ov = default_text_rules()
    assert "agent" in {t.casefold() for t in ov.exclude_terms}
    assert ov.context_cues["LOCATION"].extend == "sentence"
    assert "GB" in ov.quantity_units


def test_compile_merges_operator_exclude_and_pattern():
    extra = TextRuleOverlay.from_dict({
        "exclude_terms": ["widget"],
        "patterns": {
            "add": {
                "ops_ticket": {
                    "pattern": r"\bOPS-\d+\b",
                    "entity_type": "SUPPORT_TICKET",
                    "recognizer_group": "free_text",
                    "presidio_score": 0.9,
                    "unvalidated_reason": "operator ticket prefix",
                }
            }
        },
    })
    merged = compile_text_rules(extra)
    assert "widget" in merged.exclude_terms
    assert "agent" in {t.casefold() for t in merged.exclude_terms}
    assert "ops_ticket" in (merged.patterns.add if merged.patterns else {})


def test_term_exclusion_drops_agent_person():
    ov = default_text_rules()
    cands = [
        Candidate("PERSON", 0.9, "ner", 0, 5, "Agent"),
        Candidate("PERSON", 0.9, "ner", 7, 18, "Ahmed Hassan"),
    ]
    kept = TermExclusionFilter(ov).apply(cands, "Agent Ahmed Hassan")
    assert [c.text for c in kept] == ["Ahmed Hassan"]


def test_quantity_30_gb_is_not_masked():
    rs = RuleSetCompiler.default()
    scanner = TextScanner(ruleset=rs)
    text = "I want to have extra 30 GB of my data please"
    result = scanner.scan(
        text,
        TextScanConfig(engines="regex", language="en", min_score=0.2),
    )
    numeric = [
        d for d in result.detections
        if d.entity_type in {"PHONE_NUMBER", "EG_NATIONAL_ID", "CREDIT_CARD"}
        and d.text and "30" in (d.text or "")
    ]
    assert not numeric, numeric

    fake = Candidate("PHONE_NUMBER", 0.8, "ner", text.find("30"), text.find("30") + 2, "30")
    kept = NumberContextClassifier(rs.text_rules).apply([fake], text)
    assert kept == []


def test_operator_pattern_compiles_into_ruleset():
    overlay = TextRuleOverlay.from_dict({
        "patterns": {
            "add": {
                "ops_ticket": {
                    "pattern": r"\bOPS-\d+\b",
                    "entity_type": "SUPPORT_TICKET",
                    "recognizer_group": "free_text",
                    "script": "latin",
                    "presidio_score": 0.9,
                    "unvalidated_reason": "operator ticket prefix",
                }
            }
        }
    })
    rs = RuleSetCompiler.default(text_rules=overlay)
    assert "ops_ticket" in rs.patterns
    scanner = TextScanner(ruleset=rs)
    result = scanner.scan(
        "please follow OPS-9911 today",
        TextScanConfig(engines="regex", min_score=0.2),
    )
    tickets = [d for d in result.detections if d.entity_type == "SUPPORT_TICKET"]
    assert any(d.text == "OPS-9911" for d in tickets)


def test_operator_social_url_category_detects_trigger():
    overlay = TextRuleOverlay.from_dict({
        "context_cues": {
            "SOCIAL_URL": {"triggers": ["instagram", "linkedin.com"], "extend": "sentence"},
        },
        "patterns": {
            "add": {
                "social_url_instagram": {
                    "pattern": r"instagram(?:\.com)?",
                    "entity_type": "SOCIAL_URL",
                    "recognizer_group": "free_text",
                    "presidio_score": 0.86,
                    "unvalidated_reason": "operator-authored trigger for SOCIAL_URL",
                }
            }
        },
    })
    rs = RuleSetCompiler.default(text_rules=overlay)
    assert "social_url_instagram" in rs.patterns
    scanner = TextScanner(ruleset=rs)
    result = scanner.scan(
        "follow me on instagram tonight",
        TextScanConfig(engines="regex", min_score=0.2),
    )
    hits = [d for d in result.detections if d.entity_type == "SOCIAL_URL"]
    assert any("instagram" in (d.text or "").lower() for d in hits)


def test_llm_prompt_includes_overlay_cues():
    from redibis.pii.scan.result import TextScanConfig
    from redibis.pii.text_llm import LlmTextRefiner

    captured: dict[str, str] = {}

    class _Prov:
        def complete(self, system, user, **kw):
            captured["user"] = user
            return '{"spans":[]}'

    LlmTextRefiner(provider=_Prov()).propose_spans(
        "hello",
        [],
        TextScanConfig(use_llm=True),
        text_rules=default_text_rules(),
    )
    prompt = captured["user"]
    assert "Never flag" in prompt
    assert "LOCATION" in prompt
    assert "30 GB" in prompt or "جنيه" in prompt


def test_filters_keep_real_name_after_excluding_agent():
    text = "Agent: call Ahmed later"
    cands = [
        Candidate("PERSON", 0.95, "ner", 0, 5, "Agent"),
        Candidate("PERSON", 0.9, "ner", 12, 17, "Ahmed"),
    ]
    kept = apply_text_rule_filters(cands, text, default_text_rules())
    surfaces = [c.text for c in kept]
    assert "Agent" not in surfaces
    assert "Ahmed" in surfaces
