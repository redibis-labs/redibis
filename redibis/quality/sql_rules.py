"""
redibis.quality.sql_rules — custom SQL quality rules, one semantics everywhere.

The Quality page, the generated Jupyter program, ``QualityDraft.validate``,
``save_quality_run`` and ``quality-monitor`` all evaluate SQL rules through
this module, so a query behaves the same wherever it runs.

A SQL rule is a query over the table being checked. Refer to the table as
``${object}`` (the ODCS placeholder), ``data`` (as on the Quality page), or its
own qualified name (``shop.orders``); ``${property}`` is the rule's column.

* A query whose final ``SELECT`` starts with an aggregate
  (``SELECT COUNT(*) …``, ``SUM``, ``AVG``, ``MIN``, ``MAX``) returning one
  value is compared as that value.
* Any other query returns the rows that break the rule; their count is
  compared.

The comparison uses the ODCS threshold keys (``mustBe``, ``mustBeLessThan``,
``mustBeBetween`` …). The default is ``mustBeLessThan: 1`` — zero bad rows.

In ``RULES`` (the generated program) a SQL rule looks like::

    {"rule": "sql", "column": None,
     "kwargs": {"sql": "SELECT * FROM ${object} WHERE amount < 0",
                "mustBeLessThan": 1},
     "description": "no negative amounts"}

and in a contract (ODCS v3)::

    quality:
      - type: sql
        query: SELECT * FROM ${object} WHERE amount < 0
        mustBeLessThan: 1
        description: no negative amounts

Only a single ``SELECT`` / ``WITH`` statement is accepted. On pandas the
query runs in an in-process DuckDB with external file/network access
disabled; on Spark it runs in the DataFrame's own session against a
uniquely named temporary view (dropped afterwards).
"""

from __future__ import annotations

import datetime as _dt
import decimal
import re
import uuid
from typing import Any, Iterable, Optional

__all__ = [
    "SQL_RULE",
    "THRESHOLD_KEYS",
    "is_sql_rule",
    "sql_rule",
    "normalize_sql_rule",
    "sql_rule_to_contract",
    "sql_rule_from_contract",
    "check_query",
    "run_sql_rules",
    "add_sql_results",
]

SQL_RULE = "sql"
TABLE_VIEW = "data"
THRESHOLD_KEYS = (
    "mustBe",
    "mustNotBe",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeBetween",
    "mustNotBeBetween",
)
_DEFAULT_THRESHOLD = {"mustBeLessThan": 1}
_AGGREGATES = ("count", "sum", "avg", "min", "max", "coalesce")


# ── rule shapes ──────────────────────────────────────────────────────────────

def is_sql_rule(rule: Any) -> bool:
    """True for a RULES-style SQL rule (``rule: sql``, or a top-level ``sql`` key)."""
    if not isinstance(rule, dict):
        return False
    name = rule.get("rule") or rule.get("expectation_type") or rule.get("type")
    return name == SQL_RULE or bool(rule.get("sql"))


def _thresholds(source: dict) -> dict:
    return {k: source[k] for k in THRESHOLD_KEYS if k in source}


def sql_rule(
    query: str,
    *,
    max_failures: int = 0,
    column: Optional[str] = None,
    description: Optional[str] = None,
    **thresholds: Any,
) -> dict:
    """A RULES entry for a custom SQL rule.

    ``max_failures`` allows that many bad rows (or that count). Pass ODCS
    threshold keys instead for anything else, e.g. ``mustBeBetween=[0, 100]``.
    """
    unknown = set(thresholds) - set(THRESHOLD_KEYS)
    if unknown:
        raise ValueError(f"unknown SQL rule threshold(s): {', '.join(sorted(unknown))}; "
                         f"use one of {', '.join(THRESHOLD_KEYS)}")
    check_query(query)
    kwargs: dict[str, Any] = {"sql": query.strip()}
    kwargs.update(thresholds or {"mustBeLessThan": int(max_failures) + 1})
    out: dict[str, Any] = {"rule": SQL_RULE, "column": column, "kwargs": kwargs}
    if description:
        out["description"] = description
    return out


def normalize_sql_rule(rule: dict) -> dict:
    """Any SQL rule shape (Quality page, RULES, parsed code) → the RULES shape."""
    kwargs = dict(rule.get("kwargs") or {})
    query = (rule.get("sql") or kwargs.pop("sql", None) or kwargs.pop("query", None)
             or rule.get("query") or "")
    kwargs.pop("sql", None)
    kwargs.pop("query", None)
    max_failures = kwargs.pop("max_failures", None)
    thresholds = _thresholds(kwargs) or _thresholds(rule)
    if not thresholds:
        thresholds = ({"mustBeLessThan": int(max_failures) + 1}
                      if max_failures is not None else dict(_DEFAULT_THRESHOLD))
    out: dict[str, Any] = {
        "rule": SQL_RULE,
        "column": rule.get("column"),
        "kwargs": {"sql": str(query).strip(), **thresholds},
    }
    for key in ("description", "meta"):
        if rule.get(key):
            out[key] = rule[key]
    return out


def sql_rule_to_contract(rule: dict) -> dict:
    """RULES-shape SQL rule → ODCS ``type: sql`` quality entry."""
    r = normalize_sql_rule(rule)
    out: dict[str, Any] = {"type": SQL_RULE, "query": r["kwargs"]["sql"]}
    out.update(_thresholds(r["kwargs"]))
    if r.get("description"):
        out["description"] = r["description"]
    return out


def sql_rule_from_contract(entry: dict, column: Optional[str] = None) -> dict:
    """ODCS ``type: sql`` quality entry → RULES-shape SQL rule."""
    out: dict[str, Any] = {
        "rule": SQL_RULE,
        "column": column,
        "kwargs": {"sql": str(entry.get("query") or "").strip(),
                   **(_thresholds(entry) or dict(_DEFAULT_THRESHOLD))},
    }
    if entry.get("description"):
        out["description"] = entry["description"]
    return out


# ── query handling ───────────────────────────────────────────────────────────

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")


def check_query(query: str) -> str:
    """Return the query without a trailing ``;``; reject anything but one SELECT/WITH."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("SQL rule has no query")
    text = query.strip().rstrip(";").strip()
    bare = _STRING_RE.sub("''", _COMMENT_RE.sub(" ", text)).strip()
    if ";" in bare:
        raise ValueError("SQL rule must be a single statement")
    if not re.match(r"(?is)^\(*\s*(select|with)\b", bare):
        raise ValueError("SQL rule must be a SELECT (or WITH … SELECT) query")
    return text


def _last_select_list(query: str) -> str:
    """Text after the last ``SELECT`` at parenthesis depth 0."""
    bare = _STRING_RE.sub("''", _COMMENT_RE.sub(" ", query))
    depth, last = 0, -1
    lower = bare.lower()
    for i, ch in enumerate(bare):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif (depth == 0 and lower.startswith("select", i)
              and (i == 0 or not (lower[i - 1].isalnum() or lower[i - 1] == "_"))):
            last = i
    return bare[last + len("select"):] if last >= 0 else ""


def _is_aggregate(query: str) -> tuple[bool, bool]:
    """``(value_mode, is_count)`` for the query's final SELECT list."""
    head = _last_select_list(query).lstrip().lower()
    head = re.sub(r"^distinct\s+", "", head)
    m = re.match(r"(\w+)\s*\(", head)
    if not m or m.group(1) not in _AGGREGATES:
        return False, False
    return True, m.group(1) == "count" or bool(re.match(r"coalesce\s*\(\s*count\s*\(", head))


def _resolve(query: str, view: str, *, table: str = "", column: Optional[str] = None) -> str:
    text = query.replace("${object}", view)
    if column:
        text = text.replace("${property}", column)
    if table and "." in table:
        text = re.sub(rf"(?i)(?<![\w.`]){re.escape(table)}(?![\w`])", view, text)
        db, tbl = table.split(".", 1)
        text = re.sub(rf"(?i)`{re.escape(db)}`\s*\.\s*`{re.escape(tbl)}`", view, text)
    if view != TABLE_VIEW:
        text = re.sub(rf"(?i)\b(from|join)(\s+){TABLE_VIEW}\b", rf"\1\2{view}", text)
    return text


def _passes(value: Any, thresholds: dict) -> bool:
    if value is None:
        return False
    for key, want in thresholds.items():
        if key == "mustBe" and not value == want:
            return False
        if key == "mustNotBe" and value == want:
            return False
        if key == "mustBeGreaterThan" and not value > want:
            return False
        if key == "mustBeGreaterOrEqualTo" and not value >= want:
            return False
        if key == "mustBeLessThan" and not value < want:
            return False
        if key == "mustBeLessOrEqualTo" and not value <= want:
            return False
        if key == "mustBeBetween" and not (want[0] <= value <= want[1]):
            return False
        if key == "mustNotBeBetween" and (want[0] <= value <= want[1]):
            return False
    return True


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (list, dict, str)):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return str(value)
    return value


def _is_spark(df: Any) -> bool:
    return type(df).__module__.startswith("pyspark.") and hasattr(df, "sparkSession")


def _run_pandas(df: Any, query: str) -> tuple[list[str], list[tuple], int]:
    import duckdb

    con = duckdb.connect()
    try:
        con.register(TABLE_VIEW, df)
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        rel = con.execute(query)
        names = [d[0] for d in rel.description or []]
        rows = rel.fetchall()
        return names, rows[:5], len(rows)
    finally:
        con.close()


def _run_spark(df: Any, query: str, view: str) -> tuple[list[str], list[tuple], int]:
    spark = df.sparkSession
    df.createOrReplaceTempView(view)
    try:
        result = spark.sql(query)
        names = list(result.columns)
        first = [tuple(r) for r in result.limit(5).collect()]
        total = len(first) if len(first) < 5 else int(result.count())
        return names, first, total
    finally:
        spark.catalog.dropTempView(view)


def _evaluate(df: Any, rule: dict, *, table: str) -> dict:
    r = normalize_sql_rule(rule)
    kwargs = r["kwargs"]
    column = r.get("column")
    thresholds = _thresholds(kwargs)
    row: dict[str, Any] = {
        "column": column or "Table-Level",
        "rule": SQL_RULE,
        "success": False,
        "kwargs": dict(kwargs),
        "element_count": None if _is_spark(df) else len(df),
        "unexpected_count": 0,
        "unexpected_pct": 0.0,
        "partial_unexpected": [],
        "observed_value": None,
        "meta": dict(r.get("meta") or {}),
        "reason": "",
    }
    if r.get("description"):
        row["description"] = r["description"]
    try:
        query = check_query(kwargs.get("sql") or "")
        if _is_spark(df):
            view = f"redibis_sql_{uuid.uuid4().hex[:10]}"
            names, first, total = _run_spark(df, _resolve(query, view, table=table, column=column), view)
        else:
            names, first, total = _run_pandas(df, _resolve(query, TABLE_VIEW, table=table, column=column))
    except Exception as exc:  # noqa: BLE001 — a bad query fails its rule, not the run
        first_line = (str(exc).strip().splitlines() or [""])[0][:300]
        row["observed_value"] = f"error: {type(exc).__name__}: {first_line}"
        row["reason"] = row["observed_value"]
        return row

    value_mode, is_count = _is_aggregate(query)
    if value_mode and total == 1 and len(names) == 1:
        value = _plain(first[0][0])
        row["observed_value"] = value
        if is_count and isinstance(value, (int, float)) and not _passes(value, thresholds):
            row["unexpected_count"] = int(value)   # a failing COUNT(*) of bad rows
    else:
        value = total
        row["observed_value"] = total
        row["unexpected_count"] = total
        row["partial_unexpected"] = [
            {n: _plain(v) for n, v in zip(names, rec)} for rec in first
        ]
    if row["element_count"]:
        row["unexpected_pct"] = round(100.0 * row["unexpected_count"] / row["element_count"], 4)
    row["success"] = _passes(value, thresholds)
    if not row["success"]:
        want = ", ".join(f"{k} {v}" for k, v in thresholds.items())
        row["reason"] = (f"{total} row(s) break the rule ({want})" if not value_mode
                         else f"query returned {value!r} ({want})")
    return row


def run_sql_rules(df: Any, rules: Iterable[dict], *, table: str = "") -> list[dict]:
    """Evaluate the SQL rules among ``rules`` on a pandas or Spark DataFrame.

    Returns report rows in the gatekeeper's normalized shape (``rule``,
    ``column``, ``success``, ``kwargs``, ``unexpected_count``, ``observed_value``,
    ``partial_unexpected``, ``reason`` …). Non-SQL rules are ignored.
    """
    return [_evaluate(df, r, table=table) for r in rules or [] if is_sql_rule(r)]


def add_sql_results(report: dict, df: Any, rules: Iterable[dict], *, table: str = "") -> dict:
    """Append SQL rule results to a gatekeeper report and refresh its totals."""
    rows = run_sql_rules(df, rules, table=table)
    if not rows:
        return report
    results = list(report.get("results") or []) + rows
    ok = sum(1 for r in results if r.get("success"))
    stats = dict(report.get("statistics") or {})
    stats.update({
        "evaluated_expectations": len(results),
        "successful_expectations": ok,
        "unsuccessful_expectations": len(results) - ok,
        "success_percent": round(100.0 * ok / len(results), 4) if results else 100.0,
    })
    return {**report, "results": results, "statistics": stats,
            "success": ok == len(results)}
