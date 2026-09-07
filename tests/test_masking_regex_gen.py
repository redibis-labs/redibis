"""Regex fake-data generation — built-in generator + rstr path + API wiring."""

from __future__ import annotations

import re

import pytest

from redibis.masking.transforms import (
    KeyedRandom,
    _basic_regex_gen,
    fake_from_regex,
)
from redibis.masking.regex_library import list_patterns, resolve_pattern


def _rng(label: str = "t") -> KeyedRandom:
    return KeyedRandom(b"k" * 32, label)


# ── Built-in generator: every packaged pattern must fullmatch itself ────────

@pytest.mark.parametrize("entry", list_patterns(), ids=lambda e: e["name"])
def test_builtin_gen_fullmatches_packaged_patterns(entry):
    rng = _rng(entry["name"])
    for _ in range(25):
        out = _basic_regex_gen(rng, entry["regex"])
        assert re.fullmatch(entry["regex"], out), (
            f"{entry['name']}: generated {out!r} does not match {entry['regex']!r}"
        )


def test_quantifier_digits_are_independent():
    """\\d{8} must not repeat one digit eight times (old generator bug)."""
    rng = _rng("digits")
    outs = [_basic_regex_gen(rng, r"\d{8}") for _ in range(10)]
    assert any(len(set(o)) > 1 for o in outs), f"degenerate outputs: {outs}"


def test_alternation_does_not_truncate():
    """a|b alternation must not cut off the rest of the pattern (old bug)."""
    rng = _rng("alt")
    pattern = r"(example|test|demo)\.(com|net|org)"
    for _ in range(20):
        out = _basic_regex_gen(rng, pattern)
        assert re.fullmatch(pattern, out), out


def test_optional_plus_star_quantifiers():
    rng = _rng("quant")
    for pattern in (r"ab?c", r"a\d+b", r"x[0-9]*y"):
        for _ in range(20):
            out = _basic_regex_gen(rng, pattern)
            assert re.fullmatch(pattern, out), (pattern, out)


# ── fake_from_regex: determinism contract ────────────────────────────────────

def test_deterministic_same_seed_same_output():
    p = resolve_pattern("eg_mobile")
    a = fake_from_regex(_rng("same"), p, deterministic=True)
    b = fake_from_regex(_rng("same"), p, deterministic=True)
    assert a == b
    assert re.fullmatch(p, a)


def test_deterministic_different_label_different_output():
    p = resolve_pattern("eg_national_id")
    outs = {fake_from_regex(_rng(f"v{i}"), p, deterministic=True) for i in range(10)}
    assert len(outs) > 1  # not constant across values


def test_rstr_deterministic_path_if_installed():
    rstr = pytest.importorskip("rstr")
    # complex pattern beyond the built-in generator — must still be deterministic
    p = r"(?:[A-Z]{2,4}-)?\d{3}[a-f]+"
    a = fake_from_regex(_rng("rstr"), p, deterministic=True)
    b = fake_from_regex(_rng("rstr"), p, deterministic=True)
    assert a == b
    assert re.fullmatch(p, a)


# ── Library resolution ───────────────────────────────────────────────────────

def test_resolve_pattern_unknown_returns_none():
    assert resolve_pattern("definitely_not_registered") is None


def test_packaged_library_has_examples_matching_their_regex():
    for entry in list_patterns():
        for ex in entry.get("examples", []):
            assert re.fullmatch(entry["regex"], ex), (entry["name"], ex)


# ── Engine integration: kind=regex end to end ────────────────────────────────

def test_engine_fake_regex_library_column():
    import pandas as pd
    from redibis.masking.engine import MaskingEngine, RunKeys
    from redibis.masking.plan import MaskingPlan, ColumnMaskRule

    df = pd.DataFrame({"msisdn": ["0100000%04d" % i for i in range(5)]})
    plan = MaskingPlan(
        schema_table="t.t",
        columns=[ColumnMaskRule(
            column="msisdn", strategy="fake", deterministic=True,
            params={"kind": "regex", "regex_library": "eg_mobile"},
        )],
    )
    out = MaskingEngine(plan, RunKeys.mint(seed="s")).transform_dataframe(df)
    pat = resolve_pattern("eg_mobile")
    assert all(re.fullmatch(pat, v) for v in out["msisdn"])
    # deterministic: same seed → same outputs
    out2 = MaskingEngine(plan, RunKeys.mint(seed="s")).transform_dataframe(df)
    assert list(out["msisdn"]) == list(out2["msisdn"])


def test_engine_fake_regex_missing_pattern_raises_value_error():
    import pandas as pd
    from redibis.masking.engine import MaskingEngine, RunKeys
    from redibis.masking.plan import MaskingPlan, ColumnMaskRule

    df = pd.DataFrame({"c": ["x"]})
    plan = MaskingPlan(
        schema_table="t.t",
        columns=[ColumnMaskRule(column="c", strategy="fake",
                                params={"kind": "regex"})],  # no library/pattern
    )
    with pytest.raises(ValueError, match="regex_pattern or regex_library"):
        MaskingEngine(plan, RunKeys.mint(seed="s")).transform_dataframe(df)
