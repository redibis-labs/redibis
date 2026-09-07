"""Tests for the safe GE rule-code parser (no code execution, ever)."""

from redibis.contracts.rule_code_parser import parse_ge_rules


def test_parses_interactive_codegen_format():
    code = """
qa.add_gx_expectation(
    expectation_name='expect_column_values_to_not_be_null',
    column='phone',
    mostly=0.95)
qa.add_gx_expectation(
    expectation_name='expect_table_row_count_to_be_between',
    min_value=1, max_value=1000)
"""
    out = parse_ge_rules(code)
    assert out["errors"] == []
    assert len(out["rules"]) == 2
    r0 = out["rules"][0]
    assert r0["expectation_type"] == "expect_column_values_to_not_be_null"
    assert r0["column"] == "phone"
    assert r0["kwargs"] == {"mostly": 0.95}
    r1 = out["rules"][1]
    assert r1["column"] is None
    assert r1["kwargs"] == {"min_value": 1, "max_value": 1000}


def test_parses_direct_expect_calls():
    code = (
        "validator.expect_column_values_to_match_regex(column='email', "
        "regex=r'^\\S+@\\S+$', mostly=0.9)\n"
        "expect_column_values_to_be_unique(column='id')\n"
    )
    out = parse_ge_rules(code)
    assert len(out["rules"]) == 2
    assert out["rules"][0]["expectation_type"] == "expect_column_values_to_match_regex"
    assert out["rules"][0]["kwargs"]["regex"] == r"^\S+@\S+$"
    assert out["rules"][1]["expectation_type"] == "expect_column_values_to_be_unique"


def test_meta_is_extracted_when_dict():
    code = ("qa.add_gx_expectation(expectation_name='expect_column_values_to_be_in_set', "
            "column='status', value_set=['a','b'], meta={'severity':'P1'})")
    out = parse_ge_rules(code)
    assert out["rules"][0]["kwargs"]["value_set"] == ["a", "b"]
    assert out["rules"][0]["meta"] == {"severity": "P1"}


# ── Injection / safety ────────────────────────────────────────────────────────

def test_rejects_non_literal_argument():
    # A call that tries to smuggle code into a kwarg value must be rejected,
    # never executed, and never become a rule.
    code = ("qa.add_gx_expectation(expectation_name='expect_column_values_to_not_be_null', "
            "column=__import__('os').system('echo pwned'))")
    out = parse_ge_rules(code)
    assert out["rules"] == []
    assert out["errors"], "non-literal argument should produce an error"


def test_ignores_arbitrary_code_without_executing(tmp_path):
    # If this ever executed, the file would be created. It must NOT be.
    marker = tmp_path / "pwned.txt"
    code = (
        f"import os\n"
        f"os.system('touch {marker}')\n"
        f"open('{marker}','w').write('x')\n"
        f"qa.add_gx_expectation(expectation_name='expect_column_values_to_be_unique', column='id')\n"
    )
    out = parse_ge_rules(code)
    assert not marker.exists(), "parser must never execute pasted code"
    # The one real rule is still extracted; the rest is ignored.
    assert len(out["rules"]) == 1
    assert out["rules"][0]["expectation_type"] == "expect_column_values_to_be_unique"


def test_rejects_positional_args():
    out = parse_ge_rules("validator.expect_column_values_to_be_between('age', 0, 120)")
    assert out["rules"] == []
    assert any("keyword arguments" in e["message"] for e in out["errors"])


def test_rejects_bad_expectation_type():
    out = parse_ge_rules("qa.add_gx_expectation(expectation_name='delete_everything', column='x')")
    assert out["rules"] == []
    assert out["errors"]


def test_syntax_error_is_reported_not_raised():
    out = parse_ge_rules("qa.add_gx_expectation(expectation_name=")
    assert out["rules"] == []
    assert out["errors"] and "parse" in out["errors"][0]["message"].lower()


def test_empty_input():
    out = parse_ge_rules("")
    assert out == {"rules": [], "errors": [], "ignored": 0}


# ── Full generated program → proposed rules (one-way import) ──────────────────

def test_parses_generated_program_rules_block():
    """A complete generated program carries its rules as an inline RULES
    literal, not as expectation calls — the round trip must recover them."""
    from redibis.quality.ge_codegen import render_full_quality_program
    from redibis.quality.rule_set import QualityRuleSet

    rules = [
        {"rule": "expect_column_values_to_not_be_null", "column": "phone",
         "kwargs": {"mostly": 0.95}, "meta": {"severity": "P2"}},
        {"rule": "expect_table_row_count_to_be_between", "column": None,
         "kwargs": {"min_value": 1, "max_value": 1000}},
    ]
    program = render_full_quality_program(
        table="golden.customers", rule_set=QualityRuleSet(name="c", rules=rules),
    )
    out = parse_ge_rules(program)
    assert out["errors"] == []
    by_type = {r["expectation_type"]: r for r in out["rules"]}
    assert set(by_type) == {
        "expect_column_values_to_not_be_null",
        "expect_table_row_count_to_be_between",
    }
    phone = by_type["expect_column_values_to_not_be_null"]
    assert phone["column"] == "phone"
    assert phone["kwargs"] == {"mostly": 0.95}
    assert phone["meta"] == {"severity": "P2"}
    table_rule = by_type["expect_table_row_count_to_be_between"]
    assert table_rule["column"] is None
    assert table_rule["kwargs"] == {"min_value": 1, "max_value": 1000}


def test_parses_hand_edited_rules_block():
    code = (
        "RULES: list[dict] = [\n"
        "    {'rule': 'expect_column_values_to_be_in_set', 'column': 'status',\n"
        "     'kwargs': {'value_set': ['a', 'b']}},\n"
        "]\n"
    )
    out = parse_ge_rules(code)
    assert out["errors"] == []
    assert len(out["rules"]) == 1
    assert out["rules"][0]["kwargs"] == {"value_set": ["a", "b"]}


def test_parses_literal_quality_rule_set_call():
    code = (
        "rs = QualityRuleSet(name='c', rules=[\n"
        "    {'rule': 'expect_column_values_to_be_unique', 'column': 'id', 'kwargs': {}},\n"
        "])\n"
    )
    out = parse_ge_rules(code)
    assert [r["expectation_type"] for r in out["rules"]] == [
        "expect_column_values_to_be_unique"
    ]


def test_rules_block_and_calls_are_deduplicated():
    code = (
        "RULES: list[dict] = [\n"
        "    {'rule': 'expect_column_values_to_be_unique', 'column': 'id', 'kwargs': {}},\n"
        "]\n"
        "qa.add_gx_expectation(expectation_name='expect_column_values_to_be_unique', column='id')\n"
    )
    out = parse_ge_rules(code)
    assert len(out["rules"]) == 1


def test_rules_block_rejects_non_literal_entries(tmp_path):
    marker = tmp_path / "pwned.txt"
    code = (
        "import os\n"
        f"RULES: list[dict] = [{{'rule': 'expect_column_values_to_be_unique', "
        f"'column': open({str(marker)!r}, 'w').write('x')}}]\n"
    )
    out = parse_ge_rules(code)
    assert not marker.exists(), "parser must never execute pasted code"
    assert out["rules"] == []
    assert any("non-literal" in e["message"] for e in out["errors"])


def test_rules_block_rejects_bogus_expectation_names():
    code = "RULES: list[dict] = [{'rule': 'drop_table', 'column': 'id', 'kwargs': {}}]\n"
    out = parse_ge_rules(code)
    assert out["rules"] == []
    assert any("expectation type" in e["message"] for e in out["errors"])


def test_rules_block_rejects_non_dict_kwargs():
    code = (
        "RULES: list[dict] = [{'rule': 'expect_column_values_to_be_unique', "
        "'column': 'id', 'kwargs': 'nope'}]\n"
    )
    out = parse_ge_rules(code)
    assert out["rules"] == []
    assert any("must be a dict" in e["message"] for e in out["errors"])


def test_parser_module_never_reaches_execution_machinery():
    """The safety boundary is structural: the parser only parses. Guard it at
    the source level so a future edit cannot quietly introduce exec/eval."""
    import ast as _ast
    import inspect

    from redibis.contracts import rule_code_parser

    tree = _ast.parse(inspect.getsource(rule_code_parser))
    banned = {"exec", "eval", "compile", "__import__", "importlib", "literal_eval_unsafe"}
    called = {
        node.func.id
        for node in _ast.walk(tree)
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)
    }
    assert not (called & banned), f"parser must not call {called & banned}"
