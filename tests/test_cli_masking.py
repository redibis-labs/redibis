"""Focused CLI tests for masking locale overrides."""

from redibis.cli.main import _apply_column_faker_overrides, _parse_column_faker_overrides
from redibis.masking import ColumnMaskRule, MaskingPlan


def test_parse_column_faker_overrides_accepts_aliases():
    overrides = _parse_column_faker_overrides([
        "full_name=faker_arabic",
        "company=faker_english",
    ])
    assert overrides == {"full_name": "ar", "company": "en"}


def test_apply_column_faker_overrides_updates_fake_rule():
    plan = MaskingPlan(
        schema_table="t",
        columns=[ColumnMaskRule(
            column="full_name",
            strategy="fake",
            params={"kind": "name", "locale": "default"},
        )],
    )
    _apply_column_faker_overrides(plan, {"full_name": "ar"})
    assert plan.rule_for("full_name").params["locale"] == "ar"
