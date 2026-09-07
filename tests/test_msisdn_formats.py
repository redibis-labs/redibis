"""MSISDN format robustness — normalizer + phone-plan detection."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.thresholds import Thresholds
from redibis.config import PIIConfig


def _decide_column(df: pd.DataFrame, col: str) -> tuple[bool, str | None]:
    pii_cfg = PIIConfig(use_phonenumbers=False)
    dets = detect_pii(
        df,
        columns=[col],
        engines="regex",
        pii_config=pii_cfg,
    )
    assert len(dets) == 1
    verdict = decide_pii(dets[0], "independent", pii_cfg.thresholds)
    return verdict.detected, verdict.entity_type


@pytest.mark.parametrize(
    "col_name,values",
    [
        ("bare_10", ["1007746235"] * 20),
        ("leading_zero", ["01007746235"] * 20),
        ("bare_20", ["201007746235"] * 20),
        ("plus_20", ["+201007746235"] * 20),
        ("double_zero", ["00201007746235"] * 20),
        ("spaced", ["010 0774 6235"] * 20),
        ("dashed", ["010-0774-6235"] * 20),
        ("excel_float", ["1007746235.0"] * 20),
    ],
)
def test_msisdn_string_formats_detected(col_name, values):
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({col_name: values})
    detected, entity = _decide_column(df, col_name)
    assert detected is True
    assert entity == "PHONE_NUMBER"


def test_msisdn_int64_column_detected():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"mobile_int": [1007746235, 1012345678, 1209876543] * 7})
    detected, entity = _decide_column(df, "mobile_int")
    assert detected is True
    assert entity == "PHONE_NUMBER"


def test_random_ten_digit_not_phone():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"bad_prefix": ["9876543210", "8765432109", "7654321098"] * 7})
    detected, entity = _decide_column(df, "bad_prefix")
    assert detected is False
    assert entity != "PHONE_NUMBER"


def test_order_ids_not_phone():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"order_id": [f"ORD{i:05d}" for i in range(30)]})
    detected, _ = _decide_column(df, "order_id")
    assert detected is False


def test_phone_engine_vote_without_presidio_regex():
    """Normalizer/plan path confirms MSISDN even when raw regex misses bare-20."""
    from redibis.pii.telecom_signals import compute_msisdn_valid_rate, compute_phone_plan_score

    values = ["201007746235"] * 10
    rate = compute_msisdn_valid_rate(values)
    score, entity = compute_phone_plan_score(
        rate, {"available": False}, column_name="recipient_mobile",
    )
    assert rate == 1.0
    assert score == 1.0
    assert entity == "PHONE_NUMBER"

    d = decide_pii(
        __import__("redibis.models", fromlist=["PIIDetection"]).PIIDetection(
            column="recipient_mobile",
            detected=False,
            phone_score=score,
            entity_type=entity,
            msisdn_valid_rate=rate,
        ),
        "independent",
        Thresholds(phone_min=0.80),
    )
    assert d.detected is True
