"""Tests for engine-neutral profiling/triage helpers."""

from __future__ import annotations

import pandas as pd

from redibis.profiling import triage


def test_string_columns_filters_non_string():
    df = pd.DataFrame({
        "phone": ["+20100"],
        "amount": [1.5],
        "id": [1],
    })
    assert triage.string_columns(df) == ["phone"]


def test_profile_arabic_presence_detects_arabic():
    df = pd.DataFrame({
        "en": ["hello", "world"],
        "ar": ["hello", "مرحبا"],
    })
    scores = triage.profile_arabic_presence(df)
    assert scores["en"] == 0.0
    assert scores["ar"] == 0.5


def test_compute_column_profiles_name_hint_and_threshold():
    df = pd.DataFrame({
        "phone_number": [f"+20100{i:07d}" for i in range(10)],
        "city": ["Cairo"] * 10,
    })
    arabic = {"phone_number": 0.0, "city": 0.0}
    profiles = triage.compute_column_profiles(
        df, arabic_columns=arabic, threshold=0.0,
    )
    assert len(profiles) == 2
    phone = next(p for p in profiles if p.column == "phone_number")
    city = next(p for p in profiles if p.column == "city")
    assert phone.name_hint_score >= 1.0
    assert phone.triage_score > city.triage_score
    assert phone.send_to_detector is True
    assert profiles[0].triage_score >= profiles[1].triage_score


def test_compute_column_profiles_arabic_boosts_score():
    df = pd.DataFrame({"notes": ["plain", "plain with مرحبا"]})
    arabic = triage.profile_arabic_presence(df)
    profiles = triage.compute_column_profiles(
        df, arabic_columns=arabic, threshold=1.0,
    )
    assert profiles[0].arabic_fraction == 0.5
