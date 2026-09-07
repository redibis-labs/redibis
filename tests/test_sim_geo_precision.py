"""SIM / device / geo precision — cut false positives on numeric columns."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.config import PIIConfig
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.regex_catalog import validate_luhn


def _decide(df: pd.DataFrame, col: str) -> tuple[bool, str | None]:
    pii_cfg = PIIConfig(use_phonenumbers=False, geo_require_pair=True)
    dets = detect_pii(df, columns=[col], engines="regex", pii_config=pii_cfg)
    assert len(dets) == 1
    v = decide_pii(dets[0], "independent", pii_cfg.thresholds)
    return v.detected, v.entity_type


def _valid_imei() -> str:
    base = "35693803564380"
    for check in range(10):
        candidate = f"{base}{check}"
        if validate_luhn(candidate):
            return candidate
    raise RuntimeError("could not derive valid IMEI")


@pytest.fixture
def valid_imei():
    return _valid_imei()


def test_fifteen_digit_non_luhn_non_602_not_imsi_or_imei():
    pytest.importorskip("presidio_analyzer")
    bad = "123456789012345"
    assert not validate_luhn(bad)
    assert not bad.startswith("602")
    df = pd.DataFrame({"order_ref": [bad] * 20})
    detected, entity = _decide(df, "order_ref")
    assert detected is False or entity not in {"IMSI", "IMEI"}


def test_valid_imei_detected(valid_imei):
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"device_imei": [valid_imei] * 20})
    detected, entity = _decide(df, "device_imei")
    assert detected is True
    assert entity == "IMEI"


def test_egypt_imsi_detected():
    pytest.importorskip("presidio_analyzer")
    imsi = "602010123456789"
    df = pd.DataFrame({"imsi": [imsi] * 20})
    detected, entity = _decide(df, "imsi")
    assert detected is True
    assert entity == "IMSI"


def test_integer_age_column_not_location():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"age": list(range(18, 48))})
    detected, entity = _decide(df, "age")
    assert detected is False or entity != "LOCATION"


def test_integer_rating_column_not_location():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"rating": [1, 2, 3, 4, 5] * 10})
    detected, entity = _decide(df, "rating")
    assert detected is False or entity != "LOCATION"


def test_egypt_lat_lon_pair_detected():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({
        "latitude": ["30.0444"] * 15,
        "longitude": ["31.2357"] * 15,
    })
    pii_cfg = PIIConfig(use_phonenumbers=False, geo_require_pair=True)
    dets = detect_pii(df, columns=["latitude", "longitude"], engines="regex", pii_config=pii_cfg)
    by_col = {d.column: decide_pii(d, "independent", pii_cfg.thresholds) for d in dets}
    assert by_col["latitude"].detected is True
    assert by_col["longitude"].detected is True
    assert by_col["latitude"].entity_type == "LOCATION"


def test_gps_pair_combined_detected():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"gps": ["30.0444, 31.2357"] * 20})
    detected, entity = _decide(df, "gps")
    assert detected is True
    assert entity == "LOCATION"
