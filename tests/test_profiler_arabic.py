"""Arabic profiling expectations — GE regex compatibility and suite sync."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.models import arabic_regex_for_ge
from redibis.quality.profiler import QualityProfiler


def test_arabic_regex_for_ge_has_no_backslash_u_escapes():
    pattern = arabic_regex_for_ge()
    assert r"\u" not in pattern
    assert pattern.startswith("[")
    assert pattern.endswith("]+")


def test_profile_arabic_presence_appends_meta_expectations():
    pytest.importorskip("great_expectations")
    df = pd.DataFrame({
        "notes": ["hello", "مرحبا بالعالم", "more text"],
        "id": [1, 2, 3],
    })
    ge = QualityProfiler(df, dataset_name="customers")
    ge.run_assistant()
    before = len(ge.expectations)
    ge.profile_arabic_presence(threshold=0.05)

    assert len(ge.expectations) > before
    arabic = [
        e for e in ge.expectations
        if getattr(e, "expectation_type", "") == "expect_column_values_to_match_regex"
        and (getattr(e, "kwargs", {}) or {}).get("column") == "notes"
    ]
    assert arabic
    note = (getattr(arabic[0], "meta", {}) or {}).get("notes", {}).get("content", "")
    assert "Arabic presence" in note
