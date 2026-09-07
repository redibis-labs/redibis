"""Phone / MSISDN engine — libphonenumber gate, rates, and §3.2 scenarios."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.config import PIIConfig, resolve_pii_config
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.phone_engine import (
    name_has_phone_token,
    phone_rates,
    should_run_phone,
    structural_numeric,
)
from redibis.pii.telecom_signals import compute_phone_plan_score
from redibis.pii.thresholds import Thresholds


def test_name_has_phone_token():
    assert name_has_phone_token("recipient_mobile")
    assert name_has_phone_token("contact")
    assert not name_has_phone_token("account_id")
    assert not name_has_phone_token("created_at")


def test_should_run_phone_skips_name_column():
    values = ["Mahmoud Tarek", "Sara Ali"] * 5
    assert should_run_phone("recipient_name", values) is False


def test_should_run_phone_intl_plus_prefix():
    values = ["+442071838750", "+12025550100", "+201007746235"]
    assert should_run_phone("contact", values) is True


def test_phone_rates_international():
    values = ["+442071838750", "+12025550100", "+201007746235"]
    stats = phone_rates(values, region="EG")
    assert stats["valid_rate"] == pytest.approx(1.0, abs=0.01)
    assert stats["mobile_rate"] >= 0.66
    assert set(stats["regions"].keys()) >= {"GB", "US", "EG"}


def test_phone_rates_date_column_zero():
    values = ["2024-01-15", "15/01/2024", "2023-12-01"] * 5
    assert structural_numeric(values) is True  # gate may run
    stats = phone_rates(values, region="EG")
    assert stats["valid_rate"] == 0.0
    assert stats["mobile_rate"] == 0.0


def test_egypt_mobile_formats_detected():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"recipient_mobile": ["01007746235", "1007746235", "+201234567890"] * 7})
    pii_cfg = PIIConfig(sample_size=100)
    dets = detect_pii(df, columns=["recipient_mobile"], engines="regex", pii_config=pii_cfg)
    assert len(dets) == 1
    d = dets[0]
    assert (d.msisdn_valid_rate or 0) >= 0.80
    verdict = decide_pii(d, "independent", pii_cfg.thresholds)
    assert verdict.detected is True
    assert verdict.entity_type == "PHONE_NUMBER"


def test_international_contact_column():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({
        "contact": ["+442071838750", "+12025550100", "+201007746235"] * 5,
    })
    pii_cfg = PIIConfig()
    dets = detect_pii(df, columns=["contact"], engines="regex", pii_config=pii_cfg)
    d = dets[0]
    assert d.phone_valid_rate == pytest.approx(1.0, abs=0.05)
    verdict = decide_pii(d, "independent", pii_cfg.thresholds)
    assert verdict.detected is True
    assert verdict.entity_type == "PHONE_NUMBER"


def test_mobile_vs_account_id_same_shape():
    pytest.importorskip("presidio_analyzer")
    values = [1007746235, 1012345678, 1209876543] * 7
    pii_cfg = PIIConfig(use_phonenumbers=True)
    thresholds = pii_cfg.thresholds

    mobile_dets = detect_pii(
        pd.DataFrame({"mobile": values}),
        columns=["mobile"],
        engines="regex",
        pii_config=pii_cfg,
    )
    id_dets = detect_pii(
        pd.DataFrame({"account_id": values}),
        columns=["account_id"],
        engines="regex",
        pii_config=pii_cfg,
    )
    mobile_v = decide_pii(mobile_dets[0], "independent", thresholds)
    id_v = decide_pii(id_dets[0], "independent", thresholds)
    assert mobile_v.detected is True
    assert mobile_v.entity_type == "PHONE_NUMBER"
    assert id_v.detected is False


def test_sample_size_from_config(monkeypatch, tmp_path):
    pytest.importorskip("presidio_analyzer")
    gs_path = tmp_path / "global_settings.json"
    gs_path.write_text('{"pii": {"sample_size": 5}}', encoding="utf-8")
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))

    cfg = resolve_pii_config()
    assert cfg.sample_size == 5

    # 20 rows — detector should only see 5 when global overlay applies
    df = pd.DataFrame({"recipient_mobile": ["01007746235"] * 20})
    dets = detect_pii(df, columns=["recipient_mobile"], engines="regex", pii_config=cfg)
    assert dets[0].presidio_match_rate is not None


def test_compute_phone_plan_score_requires_context():
    score, entity = compute_phone_plan_score(
        0.95,
        {"available": False},
        column_name="account_id",
        msisdn_valid_rate_min=0.80,
    )
    assert score == pytest.approx(0.95)
    assert entity is None

    score2, entity2 = compute_phone_plan_score(
        0.95,
        {"available": False},
        column_name="customer_mobile",
        msisdn_valid_rate_min=0.80,
    )
    assert entity2 == "PHONE_NUMBER"
