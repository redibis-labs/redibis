"""Geo engine — partner-required LOCATION signals (IMEI/IMSI/geo plan)."""

from __future__ import annotations

from dataclasses import replace

from redibis.pii.context_tokens import column_has_negative_token
from redibis.pii.geo_engine import (
    detect_coordinate_scale,
    find_geo_partners,
    geo_column_signals,
    geo_confidence,
    geo_verdict,
    name_has_geo_token,
    name_suggests_latitude,
)


def test_integers_without_partner_are_none():
    """G1 regression: age-like integers never become LOCATION."""
    values = [str(i) for i in range(18, 95)]
    sig = geo_column_signals(values, partner_values=None)
    sig = replace(sig, name_signal=False, partner_signal=False)
    assert geo_verdict(sig, require_pair=True) == "none"


def test_age_negative_token_suppresses_location():
    assert column_has_negative_token("age", "LOCATION") is True
    assert column_has_negative_token("nps_score", "LOCATION") is True
    assert column_has_negative_token("cell_lat", "LOCATION") is False


def test_egypt_lat_lon_pair_geofence():
    lats = ["30.0444", "30.0500", "29.9800"] * 5
    lons = ["31.2357", "31.2400", "31.2200"] * 5
    sig = geo_column_signals(lats, partner_values=lons)
    sig = replace(sig, name_signal=True, partner_signal=True)
    assert geo_verdict(sig) in ("latitude", "both")
    assert sig.geofence_rate >= 0.80
    assert sig.egypt_geofence_hits > 0  # G3 regression
    assert geo_confidence(sig) >= 0.75


def test_pair_outside_fence_with_name_and_precision():
    lats = ["51.5074", "51.5080", "51.5060"] * 5  # London
    lons = ["-0.1278", "-0.1280", "-0.1270"] * 5
    sig = geo_column_signals(lats, partner_values=lons, geofence="egypt")
    sig = replace(sig, name_signal=True, partner_signal=True)
    assert geo_verdict(sig) in ("latitude", "both", "longitude")
    assert geo_confidence(sig) >= 0.75


def test_lone_high_precision_no_partner():
    values = ["30.0444123", "30.0500123", "29.9800123"] * 5
    sig = geo_column_signals(values, partner_values=None)
    sig = replace(sig, name_signal=True, partner_signal=False, precision_signal=True)
    assert geo_verdict(sig, require_pair=True) == "none"


def test_abs_max_over_180_is_none():
    values = ["200.12345", "201.12345"] * 5
    sig = geo_column_signals(values, partner_values=["31.23570"] * 10)
    sig = replace(sig, name_signal=True, partner_signal=True)
    assert sig.range_signal == "none"
    assert geo_verdict(sig) == "none"


def test_scaled_integers_with_partner():
    lats = [str(30_123_456 + i) for i in range(20)]
    lons = [str(31_234_567 + i) for i in range(20)]
    scale = detect_coordinate_scale(lats, lons)
    assert scale == 1_000_000
    sig = geo_column_signals(lats, partner_values=lons)
    sig = replace(
        sig,
        name_signal=True,
        partner_signal=True,
        scaled_integer=scale,
        range_signal="both",
    )
    assert geo_verdict(sig) != "none"


def test_scaled_integers_without_partner():
    lats = [str(30_123_456 + i) for i in range(20)]
    sig = geo_column_signals(lats, partner_values=None)
    sig = replace(sig, name_signal=True, partner_signal=False, scaled_integer=1_000_000)
    assert geo_verdict(sig, require_pair=True) == "none"


def test_geo_values_signals_egypt_hits_nonzero():
    from redibis.pii.telecom_signals import _geo_values_signals

    lats = ["30.0444"] * 10
    lons = ["31.2357"] * 10
    out = _geo_values_signals(lats, partner_values=lons)
    assert out["egypt_geofence_hits"] > 0


def test_false_friend_names_no_geo_signal():
    assert name_has_geo_token("latency") is False
    assert name_has_geo_token("long_description") is False
    assert name_has_geo_token("translation") is False
    assert name_has_geo_token("cell_lat") is True
    assert name_suggests_latitude("cgi_latitude") is True


def test_stem_partner_matching():
    partners = find_geo_partners(["cell_lat", "cell_lon", "other"])
    assert partners["cell_lat"] == "cell_lon"
    assert partners["cell_lon"] == "cell_lat"
