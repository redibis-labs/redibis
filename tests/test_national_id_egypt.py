"""Egyptian National ID validator + column gate (EG_NATIONAL_ID_VALIDATION_PLAN)."""

from __future__ import annotations

from datetime import date

import pytest

from redibis.models import PIIDetection
from redibis.pii.equations import decide_pii
from redibis.pii.national_id_egypt import (
    check_digit_mod11,
    name_has_nid_token,
    name_suggests_timestamp,
    nid_rates,
    normalize_nid,
    validate_egypt_national_id,
)
from redibis.pii.thresholds import Thresholds

TODAY = date(2026, 8, 6)


# ── Value level ──────────────────────────────────────────────────────────────

def test_valid_nid_extracts_demographics():
    r = validate_egypt_national_id("29001011234567", today=TODAY)
    assert r.valid is True
    assert r.birth_date == "1990-01-01"
    assert r.gov_code == "12"
    assert r.governorate == "Dakahlia"
    assert r.gender == "female"
    assert r.sequence == "3456"
    assert r.check_digit == "7"
    assert r.check_digit_ok is not None


def test_timestamp_false_positive_rejected():
    """Reported bug: YYYYMMDDHHMMSS classified as EG_NATIONAL_ID.

    ``20211015093012`` has governorate ``09`` (not in the 27-code set) so it
    fails at governorate under the ordered stages. A timestamp with a valid GG
    still fails at plausibility (birth 1900–1909 → age ≥ 117).
    """
    reported = validate_egypt_national_id("20211015093012", today=TODAY)
    assert reported.valid is False
    assert reported.reason in ("governorate", "plausibility")

    # C=2 YY=02 MM=11 DD=15 GG=01 seq=3012 check=0 → birth 1902-11-15
    r = validate_egypt_national_id("20211150130120", today=TODAY)
    assert r.valid is False
    assert r.reason == "plausibility"
    assert r.stage_reached == "plausibility"
    assert r.gov_code == "01"


def test_impossible_month_fails_calendar():
    r = validate_egypt_national_id("20240315103045", today=TODAY)
    assert r.valid is False
    assert r.reason == "calendar"


def test_feb_31_fails_calendar():
    r = validate_egypt_national_id("29902311234567", today=TODAY)
    assert r.valid is False
    assert r.reason == "calendar"


def test_leap_year_feb29():
    leap = validate_egypt_national_id("29602291234567", today=TODAY)
    assert leap.valid is True
    assert leap.birth_date == "1996-02-29"

    non_leap = validate_egypt_national_id("29702291234567", today=TODAY)
    assert non_leap.valid is False
    assert non_leap.reason == "calendar"


@pytest.mark.parametrize("gov", ["05", "20", "30", "36"])
def test_invalid_governorate(gov):
    nid = f"2900101{gov}12345"  # 14 digits: C YY MM DD GG SSSS D
    assert len(nid) == 14
    r = validate_egypt_national_id(nid, today=TODAY)
    assert r.valid is False
    assert r.reason == "governorate"


def test_future_birth_fails_plausibility():
    r = validate_egypt_national_id("39901011234567", today=TODAY)
    assert r.valid is False
    assert r.reason == "plausibility"


def test_arabic_indic_digits_normalize_and_validate():
    arabic = "٢٩٠٠١٠١١٢٣٤٥٦٧"
    assert normalize_nid(arabic) == "29001011234567"
    r = validate_egypt_national_id(arabic, today=TODAY)
    assert r.valid is True
    assert r.birth_date == "1990-01-01"


def test_spaces_dashes_nbsp_bidi_normalize():
    raw = "\u200e2900-1011\u00a02345-67\u200f"
    assert normalize_nid(raw) == "29001011234567"
    assert validate_egypt_national_id(raw, today=TODAY).valid is True


def test_letters_not_silently_dropped():
    r = validate_egypt_national_id("2A001011234567", today=TODAY)
    assert r.valid is False
    assert r.reason == "normalize"
    assert normalize_nid("2A001011234567") == ""


def test_trailing_newline_rejected():
    # Newline is not a strip char — normalize rejects rather than silently
    # truncating, which is stricter than the old ``re.match`` behaviour.
    assert normalize_nid("29001011234567\n") == ""
    r = validate_egypt_national_id("29001011234567\n", today=TODAY)
    assert r.valid is False
    assert r.reason == "normalize"


def test_int_and_float_input():
    assert validate_egypt_national_id(29001011234567, today=TODAY).valid is True
    assert validate_egypt_national_id(29001011234567.5, today=TODAY).valid is False


def test_verify_check_digit_on_off():
    base13 = "2900101123456"
    good_cd = check_digit_mod11(base13)
    good = base13 + str(good_cd)
    bad = base13 + str((good_cd + 1) % 10)

    assert validate_egypt_national_id(good, today=TODAY, verify_check_digit=True).valid
    assert not validate_egypt_national_id(bad, today=TODAY, verify_check_digit=True).valid
    assert validate_egypt_national_id(bad, today=TODAY, verify_check_digit=True).reason == "checkdigit"


def test_check_digit_ok_populated_when_disabled():
    base13 = "2900101123456"
    good = base13 + str(check_digit_mod11(base13))
    r = validate_egypt_national_id(good, today=TODAY, verify_check_digit=False)
    assert r.valid is True
    assert r.check_digit_ok is True

    bad = base13 + str((check_digit_mod11(base13) + 1) % 10)
    r2 = validate_egypt_national_id(bad, today=TODAY, verify_check_digit=False)
    # Still structurally valid when check digit is disabled
    assert r2.valid is True
    assert r2.check_digit_ok is False


def test_as_dict_and_catalog_reexport():
    from redibis.pii.regex_catalog import validate_egypt_national_id as catalog_fn

    r = catalog_fn("29001011401234")
    assert r["valid"] is True
    assert r["governorate"] == "Qalyubia"
    d = r.as_dict() if hasattr(r, "as_dict") else r
    assert d["valid"] is True


# ── Column level ─────────────────────────────────────────────────────────────

def _make_valid_ids(n: int = 100) -> list[str]:
    """Synthetic valid NIDs with diverse governorates (check digit off)."""
    govs = ["01", "02", "12", "14", "21", "22", "88"]
    out = []
    for i in range(n):
        yy = 80 + (i % 20)  # 1980–1999
        mm = 1 + (i % 12)
        dd = 1 + (i % 28)
        gov = govs[i % len(govs)]
        seq = f"{1000 + i:04d}"
        out.append(f"2{yy:02d}{mm:02d}{dd:02d}{gov}{seq}0")
    return out


def test_nid_rates_valid_column():
    rates = nid_rates(_make_valid_ids(100), today=TODAY)
    assert rates["nid_valid_rate"] == 1.0
    assert rates["nid_checked"] == 100
    assert rates["nid_gov_distinct"] >= 5
    assert rates["nid_age_p05"] is not None
    assert rates["nid_monotonic_rate"] is not None


def test_nid_rates_timestamp_column():
    # Monotonic YYYYMMDDHHMMSS sequence — all fail plausibility / governorate
    values = [f"20210101{i:06d}" for i in range(100)]
    rates = nid_rates(values, today=TODAY)
    assert rates["nid_valid_rate"] == 0.0
    assert rates["nid_checked"] == 100


def test_name_suggests_timestamp():
    assert name_suggests_timestamp("time_ts") is True
    assert name_suggests_timestamp("created_at") is True
    assert name_suggests_timestamp("event_time") is True
    assert name_suggests_timestamp("national_id") is False
    assert name_suggests_timestamp("nid") is False
    assert name_has_nid_token("national_id") is True
    assert name_has_nid_token("time_ts") is False


def test_at_suffix_suppresses_without_created_updated():
    """B1: ``at`` token must match ``record_at`` / ``closed_at`` (not only ``__at``)."""
    for name in ("record_at", "closed_at", "signup_at", "expires_at"):
        assert name_suggests_timestamp(name) is True, name
        assert name_has_nid_token(name) is False, name


def test_nid_token_not_substring_of_unidentified():
    """B2: Latin tokens are boundary-only — no ``u-nid-entified`` false positive."""
    assert name_has_nid_token("unidentified_flag") is False
    assert name_has_nid_token("is_unidentified") is False
    assert name_has_nid_token("customer_nid") is True
    assert name_has_nid_token("رقم_قومي") is True


def test_format_rejects_non_ascii_digits():
    """B3: ``\\d`` must not admit Devanagari Nd past the format stage."""
    mixed = "2९००१०११२३४५६७"
    r = validate_egypt_national_id(mixed, today=TODAY)
    assert r.valid is False
    assert r.reason == "format"


def test_nid_unique_rate_uses_format_ok_denominator():
    """B4: unique_rate shares the Stage 0+1 denominator with valid_rate."""
    ids = ["29001011234567"] * 3 + ["junk", "x", "y", "z", "a", "b", "c"]
    rates = nid_rates(ids, today=TODAY)
    assert rates["nid_checked"] == 3
    assert rates["nid_valid_rate"] == 1.0
    assert rates["nid_unique_rate"] == pytest.approx(1.0 / 3.0)


def test_calibrate_funnel_tracks_validator_stages():
    from redibis.pii.national_id_egypt import calibrate_nid_funnel

    values = (
        _make_valid_ids(5)
        + ["not-digits", "20240315103045", "29001010512345"]  # normalize / calendar / gov
    )
    funnel = calibrate_nid_funnel(values, today=TODAY)
    assert funnel["non_null"] == 8
    assert funnel["stages"]["normalize"] >= 7  # all except "not-digits"
    assert funnel["stages"]["format"] >= 7
    assert funnel["stages"]["plausibility"] == 5
    assert funnel["stages"]["calendar"] >= 5


def test_nid_rates_excludes_nulls_from_denominator():
    ids = _make_valid_ids(10) + [None, "", "N/A", "short", "123"]
    rates = nid_rates(ids, today=TODAY)
    assert rates["nid_checked"] == 10
    assert rates["nid_valid_rate"] == 1.0


# ── Equation level ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["strict", "balanced", "lenient", "independent"])
def test_nid_gate_rejects_low_rate(mode):
    d = PIIDetection(
        column="col",
        detected=False,
        entity_type="EG_NATIONAL_ID",
        presidio_score=0.95,
        nid_valid_rate=0.02,
        engine_states={"nid": {"ran": True}},
    )
    t = Thresholds(nid_min=0.85)
    assert decide_pii(d, mode, t).detected is False


@pytest.mark.parametrize("mode", ["lenient", "independent"])
def test_nid_gate_passes_high_rate(mode):
    d = PIIDetection(
        column="col",
        detected=False,
        entity_type="EG_NATIONAL_ID",
        presidio_score=0.95,
        nid_valid_rate=0.99,
        engine_states={"nid": {"ran": True}},
    )
    t = Thresholds(nid_min=0.85, presidio_min=0.80)
    assert decide_pii(d, mode, t).detected is True


@pytest.mark.parametrize("mode", ["lenient", "independent"])
def test_nid_gate_no_penalty_when_never_ran(mode):
    d = PIIDetection(
        column="col",
        detected=False,
        entity_type="EG_NATIONAL_ID",
        presidio_score=0.95,
        nid_valid_rate=None,
        engine_states={"nid": {"ran": False}},
    )
    t = Thresholds(nid_min=0.85, presidio_min=0.80)
    assert decide_pii(d, mode, t).detected is True


@pytest.mark.parametrize("mode", ["lenient", "independent"])
def test_non_nid_entity_unaffected_by_nid_min(mode):
    d = PIIDetection(
        column="email",
        detected=False,
        entity_type="EMAIL_ADDRESS",
        presidio_score=0.95,
        nid_valid_rate=0.02,
        engine_states={"nid": {"ran": True}},
    )
    t = Thresholds(nid_min=0.85, presidio_min=0.80)
    assert decide_pii(d, mode, t).detected is True


def test_loose_catalog_score_below_presidio_min():
    from redibis.pii.regex_catalog import CATALOG

    assert CATALOG["national_id_egypt"].presidio_score == 0.50
    assert CATALOG["national_id_egypt"].presidio_score < Thresholds().presidio_min
