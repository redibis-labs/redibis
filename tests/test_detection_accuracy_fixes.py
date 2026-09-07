"""Plan B — phonenumbers gating, IMEI, and network-identifier detection fixes."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.classification.pack_store import get_builtin_pack
from redibis.classification.edge_rules import apply_edge_rules_to_detection
from redibis.config import PIIConfig
from redibis.enrich.evidence import build_engine_evidence
from redibis.models import PIIDetection
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.phone_engine import name_has_phone_token, name_suggests_network_identifier
from redibis.pii.sensitivity import classify_sensitivity
from redibis.services import pipeline


@pytest.fixture
def telecom_policy():
    return get_builtin_pack("telecom")


def _pipeline(df: pd.DataFrame, columns: list[str], *, use_phonenumbers: bool = True):
    pii_cfg = PIIConfig(use_phonenumbers=use_phonenumbers)
    return pipeline.run_pii_detection(
        df,
        columns=columns,
        engines="regex",
        equation_mode="independent",
        pii_config=pii_cfg,
        edge_rules_enabled=True,
        policy_pack="telecom",
    )


def test_network_column_names_not_phone_tokens():
    assert name_suggests_network_identifier("cell_global_identity")
    assert name_suggests_network_identifier("lac")
    assert name_suggests_network_identifier("tac_lte")
    assert not name_has_phone_token("cell_global_identity")
    assert name_has_phone_token("a_party_msisdn")


def test_phonenumbers_gate_rejects_phone_regex_on_network_column():
    pytest.importorskip("presidio_analyzer")
    # Phone-shaped 10-digit values on a CGI column — must not become PHONE_NUMBER.
    df = pd.DataFrame({"cell_global_identity": ["1234567890"] * 20})
    pii_cfg = PIIConfig(use_phonenumbers=True)
    d = detect_pii(df, columns=["cell_global_identity"], engines="regex", pii_config=pii_cfg)[0]
    v = decide_pii(d, "independent", pii_cfg.thresholds)
    assert v.entity_type != "PHONE_NUMBER"
    phone_hits = [h for h in (d.regex_hits or []) if h.get("entity_type") == "PHONE_NUMBER"]
    assert not phone_hits


def test_cdr_msisdn_keeps_phone_with_valid_rate_evidence():
    pytest.importorskip("presidio_analyzer")
    from tests.generators.pandas.v1.eg_pii_pandas_generators_package.eg_pii_pandas_generators import (
        generate_telco_cdr_event,
    )

    df = generate_telco_cdr_event(25, seed=7)
    results = _pipeline(df, ["a_party_msisdn", "b_party_msisdn"])
    by_col = {d.column: d for d in results}
    for col in ("a_party_msisdn", "b_party_msisdn"):
        assert by_col[col].entity_type == "PHONE_NUMBER"
        assert by_col[col].detected is True
        assert (by_col[col].phone_valid_rate or 0) >= 0.80

    det_row = {
        "phone_valid_rate": by_col["a_party_msisdn"].phone_valid_rate,
        "phone_mobile_rate": by_col["a_party_msisdn"].phone_mobile_rate,
        "phone_regions": by_col["a_party_msisdn"].phone_regions,
        "regex_hits": by_col["a_party_msisdn"].regex_hits,
    }
    ev = build_engine_evidence({}, {}, run_detection=det_row)
    assert "phone" in ev
    assert ev["phone"]["valid_rate"] >= 0.80
    assert "mobile_rate" in ev["phone"]
    assert "regions" in ev["phone"]


def test_cdr_imsi_still_imsi():
    pytest.importorskip("presidio_analyzer")
    from tests.generators.pandas.v1.eg_pii_pandas_generators_package.eg_pii_pandas_generators import (
        generate_telco_cdr_event,
    )

    df = generate_telco_cdr_event(25, seed=7)
    results = _pipeline(df, ["imsi"])
    assert results[0].entity_type == "IMSI"
    assert results[0].detected is True


def test_cdr_imei_detected_via_name_or_luhn():
    pytest.importorskip("presidio_analyzer")
    from tests.generators.pandas.v1.eg_pii_pandas_generators_package.eg_pii_pandas_generators import (
        generate_telco_cdr_event,
    )

    df = generate_telco_cdr_event(25, seed=7)
    results = _pipeline(df, ["imei"])
    assert results[0].entity_type == "IMEI"
    assert results[0].detected is True


def test_cdr_network_identifiers_not_phone_or_personal_pii(telecom_policy):
    pytest.importorskip("presidio_analyzer")
    from tests.generators.pandas.v1.eg_pii_pandas_generators_package.eg_pii_pandas_generators import (
        generate_telco_cdr_event,
    )

    df = generate_telco_cdr_event(25, seed=7)
    results = _pipeline(df, ["cell_global_identity", "lac", "tac_lte", "cgi_latitude", "cgi_longitude"])
    by_col = {d.column: d for d in results}
    for net_col in ("cell_global_identity", "lac", "tac_lte"):
        assert by_col[net_col].entity_type == "NETWORK_ID"
        assert classify_sensitivity(by_col[net_col].entity_type) == "internal"
    assert by_col["cgi_latitude"].entity_type == "LOCATION"
    assert by_col["cgi_longitude"].entity_type == "LOCATION"


def test_device_manufacturer_and_employer_context(telecom_policy):
    """Device brand columns stay non-PII (ORGANIZATION); employer/workplace
    columns are promoted to indirect PII (EMPLOYER) — same value ('Samsung'),
    different column context, different verdict."""
    pytest.importorskip("presidio_analyzer")

    df = pd.DataFrame({
        "device_manufacturer": ["Samsung", "Apple", "Huawei", "Nokia", "Samsung"],
        "employer": [
            "Samsung Electronics", "Vodafone Egypt", "Orange Egypt",
            "Etisalat", "Samsung Electronics",
        ],
    })
    results = _pipeline(df, ["device_manufacturer", "employer"])
    by_col = {d.column: d for d in results}

    assert by_col["device_manufacturer"].entity_type == "ORGANIZATION"
    assert classify_sensitivity(by_col["device_manufacturer"].entity_type) == "internal"

    assert by_col["employer"].entity_type == "EMPLOYER"
    assert classify_sensitivity(by_col["employer"].entity_type) == "pii_indirect"


def test_imei_edge_rule_for_undetected_synthetic_column(telecom_policy):
    det = PIIDetection(column="imei", detected=False, entity_type=None, confidence=0.0)
    df = pd.DataFrame({"imei": ["490154203237000"] * 10})
    updated, result = apply_edge_rules_to_detection(det, telecom_policy, df=df)
    assert result.matched_rule_id == "imei_named_column"
    assert updated.entity_type == "IMEI"
    assert updated.detected is True
