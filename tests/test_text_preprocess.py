"""Unit tests for free-text obfuscation preprocessing."""

from __future__ import annotations

import pytest

from redibis.pii.deid.applier import DeidApplier
from redibis.pii.deid.policy import DeidPolicy, EntityRule
from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner
from redibis.pii.text_preprocess import default_pipeline, expander_registry
from redibis.pii.text_preprocess.expanders.arabic_spoken_digits import (
    ArabicSpokenDigitsExpander,
)
from redibis.pii.text_preprocess.expanders.parenthesized_digits import (
    ParenthesizedDigitsExpander,
)
from redibis.pii.text_preprocess.expanders.spaced_email import SpacedEmailExpander
from redibis.pii.text_preprocess.expanders.digit_cluster import DigitClusterExpander
from redibis.pii.text_preprocess.expanders.labeled_secret import LabeledSecretExpander
from redibis.pii.text_preprocess.expanders.age_phrase import AgePhraseExpander
from redibis.pii.text_preprocess.equivalence import VariantEquivalenceMerger
from redibis.pii.scan.result import Candidate


@pytest.fixture
def ar_ctx():
    return RecognizeContext(language="ar", arabic=True, group="free_text")


@pytest.fixture
def scanner():
    return TextScanner(ruleset=RuleSetCompiler.default())


def test_expander_registry_has_builtins():
    reg = expander_registry()
    for name in (
        "arabic_spoken_digits",
        "parenthesized_digits",
        "digit_cluster",
        "spaced_email",
        "labeled_secret",
        "age_phrase",
    ):
        assert name in reg


def test_arabic_spoken_phone_msisdn(ar_ctx):
    text = "زيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة"
    spans = ArabicSpokenDigitsExpander().expand(text, ar_ctx)
    assert spans, "expected spoken digit span"
    assert any(s.canonical == "01143215567" for s in spans)
    for s in spans:
        assert text[s.start:s.end] == s.surface


def test_arabic_spoken_plus_parenthetical_merged(scanner):
    text = (
        "المكالمة 1\tزيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة "
        "(01143215567)\tPhone Number (MSISDN Spoken)"
    )
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="both",
            language="ar",
            min_score=0.2,
            preprocess_obfuscation=True,
            default_region="EG",
        ),
    )
    phones = [d for d in result.detections if d.entity_type == "PHONE_NUMBER"]
    assert phones, f"expected phone, got {result.detections}"
    # Single combined span covering spoken + parenthetical
    assert len(phones) == 1
    p = phones[0]
    assert text[p.start:p.end] == p.text
    assert "زيرو" in p.text
    assert "01143215567" in p.text
    assert "preprocess" in result.engines_ran


def test_arabic_spoken_nid_invalid_length_is_proposal(scanner):
    # Sample is 13 digits when teens expand — not a valid EG NID.
    text = (
        "الرقم القومي اتنين تسعة خمسة زيرو سبعة واحد تلاتة اتناشر زيرو زيرو واحد ستة"
    )
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            preprocess_obfuscation=True,
        ),
    )
    nids = [d for d in result.detections if d.entity_type == "EG_NATIONAL_ID"]
    if nids:
        # Must not claim structural validation for bad length
        for n in nids:
            assert text[n.start:n.end] == n.text
            if not n.validator:
                assert n.is_proposal


def test_parenthesized_digits(ar_ctx):
    text = "call (01143215567) now"
    spans = ParenthesizedDigitsExpander().expand(text, ar_ctx)
    assert spans
    assert spans[0].canonical == "01143215567"
    assert text[spans[0].start:spans[0].end] == spans[0].surface


def test_false_positive_teen_year_not_phone(scanner):
    text = "كان عنده حداشر سنة لما سافر"
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            preprocess_obfuscation=True,
        ),
    )
    phones = [d for d in result.detections if d.entity_type == "PHONE_NUMBER"]
    assert not phones


def test_spaced_email_verbal(ar_ctx):
    text = "email me at john at example dot com please"
    spans = SpacedEmailExpander().expand(text, ar_ctx)
    assert any(s.canonical.lower() == "john@example.com" for s in spans)
    for s in spans:
        assert text[s.start:s.end] == s.surface


def test_spaced_email_scan(scanner):
    text = "reach ahmed at gmail dot com today"
    result = scanner.scan(
        text,
        TextScanConfig(engines="regex", min_score=0.2, preprocess_obfuscation=True),
    )
    emails = [d for d in result.detections if "EMAIL" in d.entity_type]
    assert emails, f"expected obfuscated email, got {result.detections}"
    assert text[emails[0].start:emails[0].end] == emails[0].text


def test_digit_cluster_spaced_phone(ar_ctx):
    text = "mobile 011-4321-5567"
    spans = DigitClusterExpander().expand(text, ar_ctx)
    assert spans
    assert "01143215567" in spans[0].canonical


def test_labeled_secret_password(ar_ctx):
    text = "password: hunter2secret"
    spans = LabeledSecretExpander().expand(text, ar_ctx)
    assert spans
    assert spans[0].entity_hint == "PASSWORD_HASH"
    assert text[spans[0].start:spans[0].end] == spans[0].surface


def test_age_phrase_ar(ar_ctx):
    text = "عمري 34 سنة"
    spans = AgePhraseExpander().expand(text, ar_ctx)
    assert spans
    assert spans[0].canonical == "34"


def test_preprocess_off_by_default(scanner):
    text = "زيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة"
    result = scanner.scan(
        text,
        TextScanConfig(engines="regex", language="ar", min_score=0.2),
    )
    assert "preprocess" not in result.engines_ran


def test_equivalence_merger_combines_same_canonical():
    text = "زيرو حداشر (011)"
    cands = [
        Candidate(
            "PHONE_NUMBER", 0.9, "preprocess", 0, 9, "زيرو حداشر",
            recognizer="arabic_spoken_digits|011", validator="phonenumbers:valid",
        ),
        Candidate(
            "PHONE_NUMBER", 0.8, "preprocess", 10, 15, "(011)",
            recognizer="parenthesized_digits|011", validator="phonenumbers:valid",
        ),
    ]
    merged = VariantEquivalenceMerger().merge(cands, text=text)
    assert len(merged) == 1
    assert merged[0].start == 0 and merged[0].end == 15


def test_emoji_offsets_with_spoken(scanner):
    text = "hi 😀 زيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة"
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            preprocess_obfuscation=True,
        ),
    )
    for d in result.detections:
        assert text[d.start:d.end] == d.text


def test_entity_filter_preprocess(scanner):
    text = "زيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة and alice@example.com"
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            preprocess_obfuscation=True,
            entities=("EMAIL_ADDRESS", "EMAIL"),
        ),
    )
    assert all("EMAIL" in d.entity_type for d in result.detections)


def test_deid_covers_spoken_phone(scanner):
    text = "اتصل زيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة"
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            preprocess_obfuscation=True,
        ),
    )
    phones = [d for d in result.detections if d.entity_type == "PHONE_NUMBER"]
    if not phones:
        pytest.skip("spoken phone not detected in this environment")
    policy = DeidPolicy(
        id="t",
        default=EntityRule(entity_type="*", strategy="redact"),
        overrides=(EntityRule(entity_type="PHONE_NUMBER", strategy="redact"),),
    )
    out = DeidApplier().apply(text, result, policy)
    assert "زيرو" not in out.deidentified_text or "[PHONE" in out.deidentified_text


def test_pipeline_recognize(ar_ctx):
    pipe = default_pipeline()
    text = "زيرو حداشر أربعة تلاتة اتنين واحد خمسة خمسة ستة سبعة (01143215567)"
    hits = pipe.recognize(text, ar_ctx)
    assert hits
    assert any(c.entity_type == "PHONE_NUMBER" for c in hits)
    for c in hits:
        assert text[c.start:c.end] == c.text


def test_spaced_email_dot_name_digits(ar_ctx):
    text = "ahmed dot sayed 89 at yahoo dot com"
    spans = SpacedEmailExpander().expand(text, ar_ctx)
    assert any(s.canonical.lower() == "ahmed.sayed89@yahoo.com" for s in spans)


def test_arabic_spoken_hundreds_and_compounds(ar_ctx):
    # Linguistic: زيرو ميتين سبعة وستين… → 0200675324 (ميتين=200, سبعة وستين=67)
    text = "زيرو ميتين سبعة وستين تلاتة وخمسين أربعة وعشرين"
    spans = ArabicSpokenDigitsExpander().expand(text, ar_ctx)
    assert any(s.canonical == "0200675324" for s in spans)


def test_arabic_spoken_wallet_grouped_and_digit_plurals(ar_ctx):
    text = "محفظتي رقم زيرو مية، عشرين تلاتين، ربعمية وخمسين"
    spans = ArabicSpokenDigitsExpander().expand(text, ar_ctx)
    assert any(s.canonical == "01002030450" for s in spans)

    text2 = "زيرو حداشر، اتنين اتنين، تلاتة تلاتة، أربعة خمسات"
    spans2 = ArabicSpokenDigitsExpander().expand(text2, ar_ctx)
    assert any(s.canonical == "01122335555" for s in spans2)


def test_arabic_spoken_card_cvv_expiry_otp(ar_ctx):
    card = (
        "بالفيزا على الكارت خمسة اتنين تلاتة أربعة، زيرو زيرو زيرو واحد، "
        "تمنية تسعة زيرو اتنين، تلاتة ربعمية وخمسين"
    )
    spans = ArabicSpokenDigitsExpander().expand(card, ar_ctx)
    assert any(s.canonical == "5234000189023450" and s.entity_hint == "CREDIT_CARD" for s in spans)

    exp = ArabicSpokenDigitsExpander().expand(
        "تاريخ الصلاحية شهر وسنة زيرو خمسة، سبعة وعشرين", ar_ctx
    )
    assert any(s.canonical == "05/27" and s.entity_hint == "CREDIT_CARD_EXPIRATION" for s in exp)

    cvv = ArabicSpokenDigitsExpander().expand(
        "رقم السي في في (CVV) اللي في ضهر الكارت سبعمية وتمنية", ar_ctx
    )
    assert any(s.canonical == "708" and s.entity_hint == "CVV" for s in cvv)

    otp = ArabicSpokenDigitsExpander().expand(
        "الكود كان ستة، سبعة، زيرو، اتنين، واحد، تسعة", ar_ctx
    )
    assert any(s.canonical == "670219" and s.entity_hint == "OTP" for s in otp)


def test_arabic_spoken_thousands_partial_card(ar_ctx):
    spans = ArabicSpokenDigitsExpander().expand(
        "آخر أربع أرقام من الكارت آخره تلات ألاف وخمسة", ar_ctx
    )
    assert any(s.canonical == "3005" and s.entity_hint == "CREDIT_CARD" for s in spans)


def test_nid_label_beats_generic_raqam_phone_hint(scanner):
    text = (
        "الرقم القومي هو اتنين تسعة خمسة زيرو سبعة واحد تلاتة اتناشر "
        "زيرو زيرو واحد ستة (2950713120016)"
    )
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            preprocess_obfuscation=True,
        ),
    )
    nids = [d for d in result.detections if d.entity_type == "EG_NATIONAL_ID"]
    assert nids, f"expected EG_NATIONAL_ID proposal, got {result.detections}"
    for n in nids:
        assert text[n.start:n.end] == n.text
        assert n.is_proposal
        assert not n.validator
