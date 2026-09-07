"""GE default-path characterization: meta notes survive suggested_rules round-trip."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from redibis.config import ProfilingConfig
from redibis.profiling.base import ProfileResult
from redibis.profiling.rules import expectations_to_rule_set
from redibis.quality.rule_set import QualityRuleSet
from redibis.scan.quality_phase import run_quality_phase
from redibis.services import pipeline
from redibis.services.scan_service import ScanConfig


def _quality_descriptions(contract: dict) -> list[str]:
    descriptions: list[str] = []
    schema = contract.get("schema") or []
    if schema:
        for prop in schema[0].get("properties") or []:
            for rule in prop.get("quality") or []:
                desc = rule.get("description")
                if desc:
                    descriptions.append(desc)
    for rule in contract.get("quality") or []:
        desc = rule.get("description")
        if desc:
            descriptions.append(desc)
    return descriptions


@pytest.fixture
def sample_df() -> pd.DataFrame:
    return pd.DataFrame({
        "notes": ["hello", "مرحبا بالعالم", "more text"],
        "id": [1, 2, 3],
    })


def test_expectations_to_rule_set_carries_meta():
    class _Exp:
        expectation_type = "expect_column_values_to_match_regex"
        kwargs = {"column": "notes", "regex": r"[\u0600-\u06FF]", "mostly": 0.05}
        meta = {
            "notes": {
                "format": "markdown",
                "content": "Arabic presence note",
            }
        }

    rs = expectations_to_rule_set([_Exp()])
    assert len(rs.rules) == 1
    assert rs.rules[0]["meta"]["notes"]["content"] == "Arabic presence note"


def test_unified_quality_path_preserves_meta_notes_in_odcs(
    sample_df: pd.DataFrame,
    tmp_path: Path,
):
    """GE expectation → rule set → apply_rules → export must keep mapper-visible notes."""
    pytest.importorskip("great_expectations")
    from great_expectations.core.expectation_configuration import ExpectationConfiguration

    arabic_note = (
        "🔹 **Arabic presence**: 33.3% of values contain Arabic characters. "
        "Activate Arabic-aware recognizers in Layer 3."
    )
    exp = ExpectationConfiguration(
        expectation_type="expect_column_values_to_match_regex",
        kwargs={
            "column": "notes",
            "regex": r".*",
            "mostly": 0.05,
        },
        meta={
            "notes": {
                "format": "markdown",
                "content": arabic_note,
            }
        },
    )
    profile = ProfileResult(
        column_profiles=[],
        arabic_columns={"notes": 0.333},
        triage_signals=[],
        suggested_rules=expectations_to_rule_set([exp]),
        raw={"engine": "great_expectations"},
    )
    assert profile.suggested_rules.rules[0].get("meta")

    config = ScanConfig(table="telecom.customers", generate_ge_docs=False)
    contract, _qa, _results, _stats = run_quality_phase(
        sample_df,
        config,
        profile,
        db_name="telecom",
        tbl_name="customers",
        run_dir=tmp_path,
        rule_set=QualityRuleSet(),
    )

    descriptions = _quality_descriptions(contract)
    assert any("Arabic presence" in d for d in descriptions), (
        f"expected Arabic meta note in ODCS descriptions, got: {descriptions}"
    )


def test_live_ge_profiler_arabic_notes_reach_odcs_contract(
    sample_df: pd.DataFrame,
    tmp_path: Path,
):
    """End-to-end: GE profiler registers Arabic expectations with meta on the suite."""
    pytest.importorskip("great_expectations")

    profile = pipeline.profile_dataframe(
        sample_df,
        "customers",
        profiling=ProfilingConfig(triage_threshold=0.0, arabic_threshold=0.05),
    )
    arabic_rules = [
        r for r in profile.suggested_rules.rules
        if r.get("meta", {}).get("notes", {}).get("content", "").startswith("🔹")
    ]
    assert arabic_rules, "Arabic expectations should be in suggested_rules"

    config = ScanConfig(table="telecom.customers", generate_ge_docs=False)
    contract, _qa, _results, _stats = run_quality_phase(
        sample_df,
        config,
        profile,
        db_name="telecom",
        tbl_name="customers",
        run_dir=tmp_path,
        rule_set=QualityRuleSet(),
    )
    descriptions = _quality_descriptions(contract)
    assert any("Arabic presence" in d for d in descriptions)
