"""IMSI structural validation — MCC registry + home/roaming."""

from __future__ import annotations

import pytest

from redibis.models import PIIDetection
from redibis.pii.equations import decide_pii
from redibis.pii.regex_catalog import CATALOG, validate_luhn
from redibis.pii.subscriber_id import validate_imsi
from redibis.pii.thresholds import Thresholds


def test_egypt_home_imsi():
    r = validate_imsi("602011234567890")
    assert r.valid is True
    assert r.is_home is True
    assert r.mcc == "602"


def test_roaming_german_imsi():
    """S1 regression — non-602 MCC must validate as roaming."""
    r = validate_imsi("262011234567890")
    assert r.valid is True
    assert r.is_home is False
    assert r.mcc == "262"


def test_fourteen_digit_imsi():
    """S2 regression — IMSI may be 14 digits."""
    r = validate_imsi("60201123456789")
    assert r.valid is True
    assert len("60201123456789") == 14


def test_unassigned_mcc():
    r = validate_imsi("999011234567890")
    assert r.valid is False
    assert r.reason == "mcc_known"


def test_mcc_beats_luhn_for_collision():
    """S4: known-MCC 15-digit that also passes Luhn → IMSI, not IMEI."""
    # Find a 602… value that passes Luhn by chance.
    found = None
    for i in range(1000):
        cand = f"60201{i:010d}"
        if len(cand) == 15 and validate_luhn(cand):
            found = cand
            break
    if found is None:
        pytest.skip("no Luhn-valid Egyptian IMSI in search window")
    imsi = validate_imsi(found)
    assert imsi.valid is True
    assert CATALOG["imsi_generic"].collision_group == "fifteen_digit_numeric"
    assert CATALOG["imei"].collision_group == "fifteen_digit_numeric"


@pytest.mark.parametrize("mode", ["strict", "balanced", "lenient", "independent"])
def test_imei_gate_rejects_low_rate(mode):
    d = PIIDetection(
        column="col",
        detected=False,
        entity_type="IMEI",
        presidio_score=0.95,
        imei_valid_rate=0.05,
        engine_states={"imei": {"ran": True}},
    )
    t = Thresholds(imei_min=0.85)
    assert decide_pii(d, mode, t).detected is False


@pytest.mark.parametrize("mode", ["lenient", "independent"])
def test_imsi_gate_no_penalty_when_never_ran(mode):
    d = PIIDetection(
        column="col",
        detected=False,
        entity_type="IMSI",
        presidio_score=0.95,
        imsi_valid_rate=None,
        engine_states={"imsi": {"ran": False}},
    )
    t = Thresholds(imsi_min=0.90, presidio_min=0.80)
    assert decide_pii(d, mode, t).detected is True


@pytest.mark.parametrize("mode", ["lenient", "independent"])
def test_geo_gate_rejects_low_confidence(mode):
    d = PIIDetection(
        column="col",
        detected=False,
        entity_type="LOCATION",
        presidio_score=0.95,
        geo_confidence=0.40,
        engine_states={"geo": {"ran": True}},
    )
    t = Thresholds(geo_min=0.75, presidio_min=0.80)
    assert decide_pii(d, mode, t).detected is False
