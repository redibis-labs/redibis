"""Tests for profile schema bootstrap partial."""

from __future__ import annotations

import pandas as pd

from redibis.contracts.schema_base import build_schema_base
from redibis.contracts.schema_partial import build_schema_partial
from redibis.contracts.type_inference import dtype_map_from_dataframe


def test_build_schema_partial_infers_types():
    df = pd.DataFrame({
        "id": [1, 2],
        "email": ["a@x.com", "b@y.com"],
        "amount": [1.5, 2.0],
    })
    partial = build_schema_partial("telecom.customers", dtype_map_from_dataframe(df), df=df)
    props = {p["name"]: p for p in partial["schema"][0]["properties"]}

    assert partial["database_name"] == "telecom"
    assert partial["table_name"] == "customers"
    assert partial["schema"][0]["physicalName"] == "telecom.customers"
    assert set(props) == {"id", "email", "amount"}
    assert props["id"]["logicalType"] == "integer"
    assert props["email"]["logicalType"] == "string"
    assert props["amount"]["logicalType"] == "number"
    assert "privacy" not in props["email"]
    assert "quality" not in props["email"]


def test_build_schema_base_phone_stays_string():
    df = pd.DataFrame({"phone": ["+201001234567", "01001234568"]})
    partial = build_schema_base("db.t", dtype_map_from_dataframe(df), df=df)
    prop = partial["schema"][0]["properties"][0]
    assert prop["logicalType"] == "string"
