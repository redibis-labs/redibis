"""Tests for column type inference in profiling reports."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd

from redibis.models import ColumnProfile
from redibis.profiling.column_types import (
    enrich_profiles_from_ge,
    enrich_profiles_from_openmetadata,
    infer_column_types_from_series,
)
from redibis.profiling import triage


def test_infer_column_types_from_series_integer():
    s = pd.Series([1, 2, 3])
    physical, logical, source = infer_column_types_from_series(s)
    assert logical == "integer"
    assert source == "pandas"
    assert "int" in physical.lower()


def test_triage_profiles_include_types():
    df = pd.DataFrame({"phone": ["+20100"], "amount": [1.5]})
    profiles = triage.compute_column_profiles(df, threshold=0.0)
    phone = next(p for p in profiles if p.column == "phone")
    assert phone.logical_type == "string"
    assert phone.type_source == "pandas"


def test_enrich_profiles_from_ge_overrides_logical_type():
    base = ColumnProfile(
        column="age",
        dtype="object",
        physical_type="string",
        logical_type="string",
        type_source="pandas",
        cardinality_ratio=0.5,
        avg_value_length=2.0,
        null_rate=0.0,
        name_hint_score=0.0,
        arabic_fraction=0.0,
        triage_score=0.5,
        send_to_detector=True,
    )
    exp = MagicMock()
    exp.expectation_type = "expect_column_values_to_be_of_type"
    exp.kwargs = {"column": "age", "type_": "Integer"}

    enriched = enrich_profiles_from_ge([base], [exp])[0]
    assert enriched.logical_type == "integer"
    assert enriched.type_source == "great_expectations"


def test_enrich_profiles_from_openmetadata_stub():
    base = ColumnProfile(
        column="balance",
        dtype="float64",
        physical_type="float64",
        logical_type="number",
        type_source="pandas",
        cardinality_ratio=0.9,
        avg_value_length=4.0,
        null_rate=0.0,
        name_hint_score=0.0,
        arabic_fraction=0.0,
        triage_score=0.2,
        send_to_detector=False,
    )
    enriched = enrich_profiles_from_openmetadata(
        [base],
        {"balance": {"logical_type": "decimal", "physical_type": "NUMERIC"}},
    )[0]
    assert enriched.logical_type == "decimal"
    assert enriched.type_source == "open_metadata"
