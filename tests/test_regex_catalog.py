"""Regex catalog validators — JWT, KSA NID, IBAN, SWIFT, UAE, MAC, BTC."""
from __future__ import annotations

import re

from redibis.pii.regex_catalog import (
    CATALOG,
    validate_btc_address,
    validate_iban,
    validate_jwt,
    validate_ksa_national_id,
    validate_mac_address,
    validate_swift_bic,
    validate_uae_national_id,
)


def test_jwt_rejects_semver_and_fqn():
    rx = re.compile(CATALOG["jwt_token"].pattern)
    for decoy in ("1.20.5", "schema.table.column", "com.example.Class", "a.b.c"):
        assert not rx.search(decoy), decoy


def test_jwt_accepts_real_token_shape():
    rx = re.compile(CATALOG["jwt_token"].pattern)
    tok = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.sig"
    assert rx.search(tok)
    assert validate_jwt(tok) is True


def test_jwt_validator_rejects_non_json_header():
    assert validate_jwt("eyJzzzz.aaaa.bbbb") is False


def test_ksa_rejects_epoch_seconds():
    for epoch in (1600000000, 1700000000, 1769999999):
        assert validate_ksa_national_id(str(epoch)) is False


def test_ksa_rejects_bad_prefix():
    assert validate_ksa_national_id("3123456789") is False


def test_ksa_accepts_luhn_valid_with_prefix():
    base = "123456789"
    for cd in range(10):
        cand = "1" + base[1:] + str(cd)
        if validate_ksa_national_id(cand):
            break
    else:
        raise AssertionError("no Luhn-valid candidate found")


def test_iban_known_good():
    assert validate_iban("GB82 WEST 1234 5698 7654 32") is True
    assert validate_iban("DE89370400440532013000") is True


def test_iban_rejects_bad_checksum():
    assert validate_iban("GB82WEST12345698765433") is False


def test_iban_rejects_wrong_country_length():
    assert validate_iban("EG" + "0" * 20) is False


def test_swift_rejects_unknown_country():
    assert validate_swift_bic("NBEGZZXX") is False
    assert validate_swift_bic("NBEGEGCX") is True


def test_uae_requires_784_prefix():
    assert validate_uae_national_id("123-1990-1234567-1") is False


def test_mac_rejects_broadcast_and_multicast():
    assert validate_mac_address("ff:ff:ff:ff:ff:ff") is False
    assert validate_mac_address("00:00:00:00:00:00") is False
    assert validate_mac_address("01:00:5e:00:00:01") is False   # multicast bit
    assert validate_mac_address("3c:22:fb:12:34:56") is True


def test_btc_checksum():
    assert validate_btc_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") is True
    assert validate_btc_address("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb") is False
