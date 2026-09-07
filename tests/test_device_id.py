"""IMEI / IMEISV structural validation (device_id)."""

from __future__ import annotations

import pytest

from redibis.pii.device_id import RBI_CODES, validate_imei, validate_imeisv
from redibis.pii.regex_catalog import CATALOG, validate_luhn


def _valid_imei_with_rbi(rbi: str = "35") -> str:
    assert rbi in RBI_CODES
    body14 = f"{rbi}693803564380"[:14]
    for check in range(10):
        candidate = f"{body14}{check}"
        if validate_luhn(candidate) and len(candidate) == 15:
            return candidate
    raise RuntimeError("could not derive Luhn-valid IMEI")


def test_known_good_imei():
    imei = _valid_imei_with_rbi("35")
    r = validate_imei(imei)
    assert r.valid is True
    assert r.tac == imei[:8]
    assert r.rbi == "35"
    assert r.stage_reached == "rbi"
    assert r.luhn_ok is True
    assert r.rbi_ok is True


def test_imei_luhn_and_rbi():
    assert validate_imei("490154203237518")
    assert not validate_imei("490154203237519")


def test_imei_rejects_random_15_digit_order_ids():
    bad = sum(
        1
        for n in range(100000000000000, 100000000000200)
        if validate_imei(str(n))
    )
    assert bad <= 20, "IMEI validator is too permissive on sequential integers"


def test_imeisv_is_not_luhn_checked_but_bounded():
    assert not validate_imeisv("1234567890123456")


def test_luhn_valid_rbi_invalid():
    body14 = "00693803564380"
    for check in range(10):
        candidate = f"{body14}{check}"
        if validate_luhn(candidate):
            r = validate_imei(candidate)
            assert r.valid is False
            assert r.reason == "rbi"
            return
    pytest.fail("could not build Luhn-valid RBI-invalid IMEI")


def test_luhn_invalid():
    r = validate_imei("351234567890120")  # almost certainly fails Luhn
    if validate_luhn("351234567890120"):
        # Try a known bad checksum
        r = validate_imei("350000000000000")
    assert r.valid is False
    assert r.reason in ("luhn", "rbi", "format")


def test_dashed_imei_normalises():
    imei = _valid_imei_with_rbi("35")
    dashed = f"{imei[0:2]}-{imei[2:8]}-{imei[8:14]}-{imei[14]}"
    r = validate_imei(dashed)
    assert r.valid is True


def test_validated_score_on_imei_catalog():
    entry = CATALOG["imei"]
    assert entry.presidio_score == 0.60
    assert entry.validated_score == 0.95


def test_imeisv_rbi():
    raw = "3569380356438001"
    r = validate_imeisv(raw)
    assert r.valid is True
    assert r.kind == "imeisv"
    assert r.svn == "01"


def test_imeisv_catalog_collision_group():
    assert CATALOG["imeisv"].collision_group == "sixteen_digit_numeric"
    assert CATALOG["credit_card_visa"].collision_group == "sixteen_digit_numeric"
    assert CATALOG["meeza_card"].collision_group == "sixteen_digit_numeric"


def test_meeza_listed_before_imeisv_in_collision_table():
    from redibis.pii.regex_catalog import COLLISION_RESOLUTION_TABLE

    keys = [k for k, _ in COLLISION_RESOLUTION_TABLE["sixteen_digit_numeric"]]
    assert keys.index("meeza_card") < keys.index("imeisv")
