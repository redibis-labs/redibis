"""Noise lexicon + NER stoplist: fillers must not break spoken-digit runs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner
from redibis.pii.text_rules import TextRuleOverlay, default_text_rules, merge_text_rules

_CFG = TextScanConfig(
    engines="regex",
    language="ar",
    min_score=0.2,
    preprocess_obfuscation=True,
    resolve="priority",
)

_FILLERS = [
    "تمام", "نعم", "افندم", "اه", "اهه", "ايوة", "تماما", "ok", "yes", "mm",
]


def _scan(text: str, *, overlay=None, cfg=_CFG, ner_backend=None):
    if overlay is None:
        ruleset = RuleSetCompiler.default()
    else:
        ruleset = RuleSetCompiler.default(text_rules=overlay)
    return TextScanner(ruleset=ruleset, ner_backend=ner_backend).scan(text, cfg)


def _assert_slice_integrity(text, result):
    for d in result.detections:
        if d.start is None or d.end is None:
            continue
        assert text[d.start:d.end] == d.text


def test_filler_between_spoken_digits_does_not_break_the_run():
    text = "الرقم زيرو واحد واحد ايوة تمام خمسة اتنين تلاتة أربعة خمسة ستة سبعة."
    r = _scan(text)
    _assert_slice_integrity(text, r)
    phones = [d for d in r.detections if d.entity_type == "PHONE_NUMBER"]
    assert phones, "filler split the spoken-digit run"
    digits = "".join(ch for ch in (phones[0].canonical or "") if ch.isdigit())
    assert digits.startswith("0115")
    assert "5234567" in digits or digits == "0115234567"
    # Leading/trailing fillers are stripped; a middle filler sits inside a
    # contiguous Unicode span and cannot be excluded without breaking
    # text[start:end] == d.text.
    assert text[phones[0].start:phones[0].end] == phones[0].text


@pytest.mark.parametrize("filler", _FILLERS)
def test_every_default_filler_is_transparent(filler):
    text = f"الرقم زيرو واحد واحد {filler} خمسة اتنين تلاتة أربعة خمسة ستة سبعة."
    r = _scan(text)
    _assert_slice_integrity(text, r)
    phones = [d for d in r.detections if d.entity_type == "PHONE_NUMBER"]
    assert phones, f"{filler!r} split the spoken-digit run"
    canon = phones[0].canonical or ""
    assert filler not in canon
    assert "ايوة" not in canon


def test_leading_and_trailing_fillers_are_outside_the_span():
    text = "الرقم تمام زيرو واحد واحد خمسة اتنين تلاتة أربعة خمسة ستة سبعة نعم."
    r = _scan(text)
    _assert_slice_integrity(text, r)
    phones = [d for d in r.detections if d.entity_type == "PHONE_NUMBER"]
    assert phones
    surface = text[phones[0].start:phones[0].end]
    assert not surface.startswith("تمام")
    assert "نعم" not in surface


def test_noise_only_span_is_dropped():
    text = "ايوة تمام نعم"
    r = _scan(text)
    _assert_slice_integrity(text, r)
    leftover = [
        d for d in r.detections
        if (d.text or "").strip() in {"ايوة", "تمام", "نعم"}
    ]
    assert not leftover


def test_ner_stoplist_suppresses_a_person_hit(monkeypatch):
    class Stub:
        def analyze_text(self, text, labels=None, phrases=None):
            i = text.find("Agent")
            if i < 0:
                return []
            return [SimpleNamespace(
                start=i, end=i + 5, label="PERSON", score=0.99, text="Agent", model="stub",
            )]

    text = "Agent called about the bill"
    overlay = merge_text_rules(default_text_rules(), {
        "ner_stoplist": {"*": ["agent", "caller"]},
    })
    cfg = TextScanConfig(engines="ner", language="en", min_score=0.1)
    r = _scan(text, overlay=overlay, cfg=cfg, ner_backend=Stub())
    _assert_slice_integrity(text, r)
    people = [d for d in r.detections if d.entity_type == "PERSON"]
    assert not people


def test_noise_stripping_never_violates_slice_integrity():
    texts = [
        "الرقم زيرو واحد ايوة خمسة اتنين تلاتة أربعة خمسة ستة سبعة.",
        "Call 01522345678.",
        "Contact alice@example.com please",
        "تمام",
    ]
    for text in texts:
        r = _scan(text, cfg=TextScanConfig(
            engines="regex",
            language="ar" if any("\u0600" <= ch <= "\u06ff" for ch in text) else "en",
            min_score=0.2,
            preprocess_obfuscation=True,
        ))
        _assert_slice_integrity(text, r)


def test_operator_added_noise_term_takes_effect_without_restart():
    nonce = "بلوبxyz"
    text = f"الرقم زيرو واحد {nonce} خمسة اتنين تلاتة أربعة خمسة ستة سبعة."
    baseline = _scan(text)
    full = "015234567"
    base_digits = [
        "".join(ch for ch in (d.canonical or "") if ch.isdigit())
        for d in baseline.detections if d.entity_type == "PHONE_NUMBER"
    ]
    assert full not in base_digits

    overlay = merge_text_rules(default_text_rules(), TextRuleOverlay.from_dict({
        "noise_terms": [nonce],
    }))
    r = _scan(text, overlay=overlay)
    _assert_slice_integrity(text, r)
    digits = [
        "".join(ch for ch in (d.canonical or "") if ch.isdigit())
        for d in r.detections if d.entity_type == "PHONE_NUMBER"
    ]
    assert full in digits, digits


def test_default_overlay_ships_noise_terms_and_ner_stoplist():
    rules = default_text_rules()
    folded = {t.casefold() for t in rules.noise_terms}
    for term in ("ايوة", "تمام", "ok", "mm", "yes"):
        assert any(term.casefold() == t or term in rules.noise_terms for t in rules.noise_terms)
    assert "ايوة" in rules.noise_terms
    assert folded.issuperset({"ok", "mm", "yes"})
    assert "agent" in rules.ner_stoplist.get("*", ())
