"""
redibis.contracts.rule_code_parser — SAFE parser for pasted GE rule code.

Turns pasted Great Expectations rule *text* into structured rule dicts WITHOUT
executing any of it. This is the security boundary for the "paste rules" feature:
the only thing we ever evaluate later is a recognized GE expectation against the
sampled data — never arbitrary Python.

How it stays safe:
  - We `ast.parse()` the text (parse only — never `compile`/`exec`/`eval`).
  - We walk the AST and extract ONLY recognized expectation calls:
        qa.add_gx_expectation(expectation_name='expect_...', column='c', mostly=0.9)
        validator.expect_column_values_to_not_be_null(column='c')
        expect_column_values_to_match_regex(column='c', regex='...')
    …plus the inline rule-list literal a generated full program carries, so
    code copied from the UI and edited in Jupyter can come back as rules:
        RULES: list[dict] = [{'rule': 'expect_...', 'column': 'c', 'kwargs': {}}]
        QualityRuleSet(rules=[{'rule': 'expect_...', ...}])
    …and custom SQL rules, kept as text (the query is never run here):
        {'rule': 'sql', 'kwargs': {'sql': 'SELECT ...', 'mustBeLessThan': 1}}
        sql_rule('SELECT ...', description='...')     RULES.append(...)
    …and the ``QualityDraft`` style the Quality page and ``to_code()`` write,
    where the column may be the one positional argument and a severity rides along:
        qa.expect_column_values_to_be_unique('id', severity='P2')
        qa.add_sql('SELECT ...', description='...', severity='P1')
  - Every argument value must be a literal (str/num/bool/None/list/dict/tuple).
    `ast.literal_eval` is applied to each argument node; anything non-literal
    (a name, attribute, function call, comprehension, f-string, …) is REJECTED
    for that rule. So a pasted ``__import__('os').system('rm -rf /')`` never runs
    and never becomes a rule — it is simply reported as an error/ignored.
  - The expectation type must match ``^expect_[a-z0-9_]+$``.

Output (no I/O, no GE import):
    {
      "rules":  [{"expectation_type", "column", "kwargs", "meta"}],
      "errors": [{"line", "message", "snippet"}],
      "ignored": <count of statements that were not rule calls>,
    }

`rules` entries are shaped exactly like the input ``quality_row_to_fragment``
expects, so they can flow straight into the approved-quality API.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

# Direct expectation calls look like expect_<something>. GE naming convention.
_EXPECT_RE = re.compile(r"^expect_[a-z0-9_]+$")

# The wrapper used by the interactive review codegen.
_ADD_FUNCS = frozenset({"add_gx_expectation", "add_expectation"})

# Inline rule-list blocks emitted by ``render_full_quality_program``.
_RULE_LIST_NAMES = frozenset({"RULES", "QUALITY_RULES"})
_RULE_SET_CALLS = frozenset({"QualityRuleSet"})

# Curated "known/portable" expectation types — used only to flag a rule as
# recognized for the UI. Any well-formed expect_* name is still accepted
# (validation happens at evaluate time against the installed GE version).
KNOWN_GE_EXPECTATIONS: frozenset[str] = frozenset({
    "expect_column_values_to_not_be_null",
    "expect_column_values_to_be_null",
    "expect_column_values_to_be_unique",
    "expect_column_values_to_be_in_set",
    "expect_column_values_to_not_be_in_set",
    "expect_column_values_to_match_regex",
    "expect_column_values_to_not_match_regex",
    "expect_column_values_to_be_between",
    "expect_column_value_lengths_to_be_between",
    "expect_column_values_to_be_of_type",
    "expect_column_values_to_be_increasing",
    "expect_column_values_to_be_decreasing",
    "expect_column_unique_value_count_to_be_between",
    "expect_table_row_count_to_be_between",
    "expect_table_row_count_to_equal",
    "expect_table_columns_to_match_set",
    "expect_column_mean_to_be_between",
    "expect_column_median_to_be_between",
    "expect_column_min_to_be_between",
    "expect_column_max_to_be_between",
    "expect_column_sum_to_be_between",
    "expect_column_stdev_to_be_between",
    "expect_column_proportion_of_unique_values_to_be_between",
})


class RuleParseError(ValueError):
    """Raised only for a hard, whole-text parse failure (SyntaxError)."""


def _literal(node: ast.AST) -> Any:
    """Return the literal value of an AST node, or raise ValueError if it is not
    a pure literal. This is what blocks code injection: names, calls, attributes,
    f-strings, comprehensions, etc. all raise here and the rule is rejected.
    """
    return ast.literal_eval(node)   # raises ValueError/SyntaxError on non-literals


def _callee_name(call: ast.Call) -> Optional[str]:
    """Method/function name of a Call node (``qa.add_gx_expectation`` → name)."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _line_snippet(code_lines: list[str], lineno: int) -> str:
    if 1 <= lineno <= len(code_lines):
        return code_lines[lineno - 1].strip()[:200]
    return ""


_SEVERITIES = ("P1", "P2", "P3")


def _sql_rule_call(call: ast.Call, lineno: int, snippet: str,
                   name: str = "sql_rule") -> tuple[Optional[dict], Optional[dict]]:
    """``sql_rule('SELECT …', max_failures=0, column=…, description=…)`` → rule.

    ``qa.add_sql(…)`` takes the same arguments plus ``severity=``.
    """
    from redibis.quality.sql_rules import sql_rule

    try:
        args = [_literal(a) for a in call.args]
        kwargs = {kw.arg: _literal(kw.value) for kw in call.keywords if kw.arg}
    except (ValueError, SyntaxError, TypeError):
        return None, {"line": lineno,
                      "message": f"{name}(): arguments must be literal values",
                      "snippet": snippet}
    if len(args) > 1 or any(kw.arg is None for kw in call.keywords):
        return None, {"line": lineno,
                      "message": f"{name}(): pass the query, then keyword arguments",
                      "snippet": snippet}
    if args:
        kwargs["query"] = args[0]
    severity = kwargs.pop("severity", None)
    try:
        built = sql_rule(**kwargs)
    except (TypeError, ValueError) as exc:
        return None, {"line": lineno, "message": f"{name}(): {exc}", "snippet": snippet}
    rule, error = _normalize_rule_mapping(built, lineno, snippet)
    if rule is not None and severity in _SEVERITIES:
        rule.setdefault("meta", {})["severity"] = severity
    return rule, error


def _is_rules_append(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr == "append"
            and isinstance(func.value, ast.Name) and func.value.id in _RULE_LIST_NAMES)


def _extract_call(call: ast.Call, code_lines: list[str]) -> tuple[Optional[dict], Optional[dict]]:
    """Try to turn one Call node into a rule dict.

    Returns (rule, error). Exactly one is non-None, or both None when the call is
    simply not an expectation call (ignored).
    """
    name = _callee_name(call)
    if name is None:
        return None, None

    lineno = getattr(call, "lineno", 0)
    snippet = _line_snippet(code_lines, lineno)

    if name == "sql_rule":
        return _sql_rule_call(call, lineno, snippet)
    if name == "add_sql" and isinstance(call.func, ast.Attribute):
        return _sql_rule_call(call, lineno, snippet, name="add_sql")
    if _is_rules_append(call) and len(call.args) == 1 and isinstance(call.args[0], ast.Dict):
        try:
            return _normalize_rule_mapping(_literal(call.args[0]), lineno, snippet)
        except (ValueError, SyntaxError, TypeError):
            return None, {"line": lineno,
                          "message": "RULES.append(): the rule must be a dict literal",
                          "snippet": snippet}

    is_add = name in _ADD_FUNCS
    is_expect = bool(_EXPECT_RE.match(name))
    if not (is_add or is_expect):
        return None, None   # not a rule call → ignore

    # Positional args are ambiguous without GE introspection — and a common
    # injection vector — so only one is accepted: the column name, as a string
    # literal, first (``qa.expect_column_values_to_be_unique('id')``).
    kwargs: dict[str, Any] = {}
    positional_column = None
    if call.args:
        first = call.args[0]
        if (is_add or len(call.args) > 1 or not isinstance(first, ast.Constant)
                or not isinstance(first.value, str)):
            return None, {"line": lineno,
                          "message": f"{name}(): pass the column name first, then keyword "
                                     "arguments only (e.g. 'x', mostly=0.95)",
                          "snippet": snippet}
        positional_column = first.value

    for kw in call.keywords:
        if kw.arg is None:   # **kwargs splat — not a literal, reject
            return None, {"line": lineno,
                          "message": f"{name}(): **kwargs expansion is not allowed",
                          "snippet": snippet}
        try:
            kwargs[kw.arg] = _literal(kw.value)
        except (ValueError, SyntaxError, TypeError):
            return None, {"line": lineno,
                          "message": f"{name}(): argument {kw.arg!r} is not a "
                                     "literal value (only strings/numbers/lists/"
                                     "dicts/booleans/None are allowed)",
                          "snippet": snippet}

    # Resolve the expectation type.
    if is_add:
        etype = kwargs.pop("expectation_name", None) or kwargs.pop("expectation_type", None)
        if not isinstance(etype, str):
            return None, {"line": lineno,
                          "message": f"{name}(): missing string expectation_name",
                          "snippet": snippet}
    else:
        etype = name

    if not _EXPECT_RE.match(etype):
        return None, {"line": lineno,
                      "message": f"unrecognized expectation type {etype!r} "
                                 "(must look like expect_...)",
                      "snippet": snippet}

    if positional_column is not None:
        if "column" in kwargs:
            return None, {"line": lineno,
                          "message": f"{name}(): the column is given twice",
                          "snippet": snippet}
        kwargs["column"] = positional_column
    meta = kwargs.pop("meta", None)
    if meta is not None and not isinstance(meta, dict):
        meta = None
    severity = kwargs.pop("severity", None)
    if severity in _SEVERITIES:
        meta = {**(meta or {}), "severity": severity}
    column = kwargs.pop("column", None)
    # batch_id is GE plumbing, never a user rule param.
    kwargs.pop("batch_id", None)

    rule = {
        "expectation_type": etype,
        "column": column if isinstance(column, str) else None,
        "kwargs": kwargs,
    }
    if meta:
        rule["meta"] = meta
    return rule, None


def _normalize_rule_mapping(
    value: Any, lineno: int, snippet: str
) -> tuple[Optional[dict], Optional[dict]]:
    """Normalize one ``{"rule": ..., "column": ..., "kwargs": {...}}`` mapping
    from a generated program's ``RULES`` block into the parsed-rule shape."""
    if not isinstance(value, dict):
        return None, {"line": lineno,
                      "message": "rule list entries must be dict literals",
                      "snippet": snippet}

    etype = value.get("rule") or value.get("expectation_type") or value.get("expectation_name")
    if etype == "sql" or (etype is None and isinstance(value.get("sql"), str)):
        from redibis.quality.sql_rules import normalize_sql_rule

        normalized = normalize_sql_rule(value)
        if not isinstance(normalized["kwargs"].get("sql"), str) or not normalized["kwargs"]["sql"]:
            return None, {"line": lineno, "message": "sql rule has no query ('sql')",
                          "snippet": snippet}
        rule = {"expectation_type": "sql",
                "column": normalized["column"] if isinstance(normalized["column"], str) else None,
                "kwargs": normalized["kwargs"]}
        if isinstance(value.get("description"), str) and value["description"]:
            rule["description"] = value["description"]
        return rule, None
    if not isinstance(etype, str) or not _EXPECT_RE.match(etype):
        return None, {"line": lineno,
                      "message": f"rule entry has no valid expectation type "
                                 f"(got {etype!r}; must look like expect_...)",
                      "snippet": snippet}

    kwargs = value.get("kwargs") or {}
    if not isinstance(kwargs, dict):
        return None, {"line": lineno,
                      "message": f"{etype}: 'kwargs' must be a dict literal",
                      "snippet": snippet}
    kwargs = {k: v for k, v in kwargs.items() if isinstance(k, str)}
    kwargs.pop("batch_id", None)
    # ``column`` may sit at either level; the top level wins.
    kwargs.pop("column", None)

    column = value.get("column")
    meta = value.get("meta")

    rule = {
        "expectation_type": etype,
        "column": column if isinstance(column, str) else None,
        "kwargs": kwargs,
    }
    if isinstance(meta, dict) and meta:
        rule["meta"] = meta
    return rule, None


def _rule_list_nodes(tree: ast.AST) -> list[ast.AST]:
    """Locate candidate rule-list nodes in a generated full program.

    Recognizes ``RULES = [...]`` / ``RULES: list[dict] = [...]`` and a literal
    ``QualityRuleSet(rules=[...])``. Purely structural — the values themselves
    are still checked by ``ast.literal_eval``.
    """
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if targets and node.value is not None:  # type: ignore[union-attr]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in _RULE_LIST_NAMES:
                    nodes.append(node.value)  # type: ignore[union-attr]
                    break
        if isinstance(node, ast.Call) and _callee_name(node) in _RULE_SET_CALLS:
            for kw in node.keywords:
                if kw.arg == "rules":
                    nodes.append(kw.value)
    return nodes


def _extract_rule_lists(tree: ast.AST, code_lines: list[str]) -> tuple[list[dict], list[dict], int]:
    """Extract rules from every recognized rule-list block.

    Returns ``(rules, errors, blocks_seen)``. A block whose value is not a pure
    literal list (e.g. the generated ``[dict(r) for r in RULES]`` comprehension)
    is skipped silently — the authoritative ``RULES`` literal carries the data.
    """
    rules: list[dict] = []
    errors: list[dict] = []
    blocks = 0
    for value_node in _rule_set_nodes_sorted(tree):
        if not isinstance(value_node, (ast.List, ast.Tuple)):
            continue
        lineno = getattr(value_node, "lineno", 0)
        blocks += 1
        for elt in value_node.elts:
            item_lineno = getattr(elt, "lineno", lineno)
            snippet = _line_snippet(code_lines, item_lineno)
            if isinstance(elt, ast.Call) and _callee_name(elt) == "sql_rule":
                rule, error = _sql_rule_call(elt, item_lineno, snippet)
            else:
                try:
                    item = _literal(elt)
                except (ValueError, SyntaxError, TypeError):
                    errors.append({
                        "line": item_lineno,
                        "message": "rule list contains non-literal values (only strings/"
                                   "numbers/lists/dicts/booleans/None are allowed)",
                        "snippet": snippet,
                    })
                    continue
                rule, error = _normalize_rule_mapping(item, item_lineno, snippet)
            if rule is not None:
                rules.append(rule)
            elif error is not None:
                errors.append(error)
    return rules, errors, blocks


def _rule_set_nodes_sorted(tree: ast.AST) -> list[ast.AST]:
    return sorted(_rule_list_nodes(tree), key=lambda n: getattr(n, "lineno", 0))


def _rule_identity(rule: dict) -> str:
    payload = {
        "expectation_type": rule.get("expectation_type"),
        "column": rule.get("column"),
        "kwargs": rule.get("kwargs") or {},
    }
    return json.dumps(payload, sort_keys=True, default=str)


def parse_ge_rules(code: str) -> dict:
    """Safely parse pasted GE rule code into structured rules.

    Handles both the AST-safe paste fragment (``add_gx_expectation(...)`` /
    ``expect_*(...)`` calls) and a complete generated program's inline
    ``RULES = [...]`` literal, so code copied from the UI and extended in
    Jupyter can be brought back as proposed rules.

    Never executes the code. See module docstring for the safety model.
    """
    code = code or ""
    result: dict[str, Any] = {"rules": [], "errors": [], "ignored": 0}
    if not code.strip():
        return result

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        result["errors"].append({
            "line": e.lineno or 0,
            "message": f"Could not parse the pasted text: {e.msg}",
            "snippet": (e.text or "").strip()[:200],
        })
        return result

    code_lines = code.splitlines()

    block_rules, block_errors, _blocks = _extract_rule_lists(tree, code_lines)
    result["errors"].extend(block_errors)

    seen: set[str] = set()
    matched = 0
    for rule in block_rules:
        identity = _rule_identity(rule)
        if identity in seen:
            continue
        seen.add(identity)
        result["rules"].append(rule)
        matched += 1

    call_nodes = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)),
                        key=lambda n: (n.lineno, n.col_offset,       # source order; a chain
                                       n.end_lineno or 0, n.end_col_offset or 0))   # qa.a().b(): a first
    for call in call_nodes:
        rule, error = _extract_call(call, code_lines)
        if rule is not None:
            identity = _rule_identity(rule)
            if identity in seen:
                continue
            seen.add(identity)
            result["rules"].append(rule)
            matched += 1
        elif error is not None:
            result["errors"].append(error)

    # Statements that were not recognized expectation calls (comments, blank
    # lines, assignments, etc.) — informational only.
    total_stmts = len([n for n in ast.walk(tree) if isinstance(n, ast.stmt)])
    result["ignored"] = max(0, total_stmts - matched)
    return result
