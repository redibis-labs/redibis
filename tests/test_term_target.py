"""Term-target advisor uses the real filters."""

from __future__ import annotations

import inspect

from redibis.pii.rules.term_target import advise, ineffective_terms, summarize_advice
from redibis.pii.rules.text_filters import TermExclusionFilter
from redibis.pii.scan.result import Candidate
from redibis.pii.text_rules import TextRuleOverlay, default_text_rules, merge_text_rules
from redibis.services import text_pii_service as svc_mod


def _spans(text, surface, et="PERSON", engine="ner"):
    start = text.index(surface)
    end = start + len(surface)
    return [Candidate(et, 0.9, engine, start, end, surface)]


def test_advise_multitoken_never_recommends_noise_terms():
    text = "قابل محمد علي اليوم"
    spans = _spans(text, "محمد علي")
    rows = advise("محمد علي", text=text, spans=spans)
    noise = next(r for r in rows if r.target == "noise_terms")
    assert noise.would_remove == ()
    assert not noise.default
    assert "more than one token" in noise.reason


def test_advise_reports_zero_effect_for_word_inside_longer_span():
    text = "قابل محمد علي اليوم"
    spans = _spans(text, "محمد علي")
    rows = advise("محمد", text=text, spans=spans)
    exclude = next(r for r in rows if r.target == "exclude_terms")
    assert exclude.would_remove == ()
    assert "sits inside" in exclude.reason
    viable = [r for r in rows if r.would_remove]
    assert not any(r.target == "noise_terms" and r.would_remove for r in viable)
    payload = summarize_advice(rows)
    assert payload["effective"] is False
    assert payload["reason"]
    assert "sits inside" in payload["reason"]


def test_advise_envelope_effective_when_a_target_would_drop():
    text = "hello Alice"
    spans = _spans(text, "Alice")
    payload = summarize_advice(advise("Alice", text=text, spans=spans, requested="exclude_terms"))
    assert payload["effective"] is True
    assert payload["reason"] == ""
    assert payload["advice"]


def test_advise_uses_real_filters(monkeypatch):
    text = "hello Alice"
    spans = _spans(text, "Alice")
    baseline = advise("Alice", text=text, spans=spans, requested="exclude_terms")
    assert next(r for r in baseline if r.target == "exclude_terms").would_remove

    def never_drop(self, candidates, text=""):
        return list(candidates)

    monkeypatch.setattr(TermExclusionFilter, "apply", never_drop)
    patched = advise("Alice", text=text, spans=spans, requested="exclude_terms")
    assert next(r for r in patched if r.target == "exclude_terms").would_remove == ()


def test_dry_run_reports_ineffective_terms():
    text = "قابل محمد علي اليوم"
    spans = _spans(text, "محمد علي")
    flagged = ineffective_terms(
        {"exclude_terms": ["محمد"], "noise_terms": ["محمد علي"]},
        text=text,
        spans=spans,
    )
    pairs = {(row["field"], row["term"]) for row in flagged}
    assert ("exclude_terms", "محمد") in pairs
    assert ("noise_terms", "محمد علي") in pairs


def test_saved_rules_take_effect_on_next_scan_without_restart():
    """A freshly compiled ruleset (the post-save `_svc()` rebuild) honours exclude_terms."""
    from redibis.pii.rules.ruleset import RuleSetCompiler
    from redibis.pii.scan.result import TextScanConfig
    from redibis.pii.scan.text_scanner import TextScanner

    text = "mail alice@example.com please"
    first = TextScanner(ruleset=RuleSetCompiler.default()).scan(
        text, TextScanConfig(engines="regex", min_score=0.2),
    )
    emails = [d for d in first.detections if d.entity_type == "EMAIL_ADDRESS"]
    assert emails, first.detections
    surface = emails[0].text
    overlay = merge_text_rules(
        default_text_rules(),
        TextRuleOverlay.from_dict({"exclude_terms": [surface]}),
    )
    second = TextScanner(ruleset=RuleSetCompiler.default(text_rules=overlay)).scan(
        text, TextScanConfig(engines="regex", min_score=0.2),
    )
    leftover = [
        d for d in second.detections
        if d.entity_type == "EMAIL_ADDRESS" and d.text == surface
    ]
    assert leftover == []
    src = inspect.getsource(svc_mod.text_pii_service_from_env)
    assert "lru_cache" not in src
    assert not hasattr(svc_mod.text_pii_service_from_env, "cache_info")
    from redibis.services.text_pii_service import TextPIIService
    assert hasattr(TextPIIService, "reload_text_rules")
