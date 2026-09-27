"""Fused Spark validation: many quality rules, one Spark job.

Great Expectations computes each expectation's metrics separately; on Spark that
is several jobs per rule, and each job reads the source again unless the frame
is persisted. This module compiles the rules instead into **aggregate
expressions of a single ``df.agg(...)``**:

    rule 3  expect_column_values_to_not_be_null(email)   → r3__bad = count(*) - count(email)
    rule 7  expect_column_values_to_be_between(qty, 1, 20) → r7__bad = sum(qty < 1 OR qty > 20)
    rule 9  expect_column_mean_to_be_between(price, …)     → r9__v   = avg(price)
    …       all in one  SELECT count(*), r3__bad, r7__bad, r9__v, … FROM partition

Spark returns one row. Every value in it is named after the rule it belongs to
(``r<i>__…``), so the row is split back into **one result per rule**: the same
``success / unexpected_count / observed_value`` rows Great Expectations
produces, which the rest of redibis (validation results, the partition result
store) records unchanged: one record per table × partition × rule.

Samples of failing values are taken in a second, small job, only when rules
fail (``samples=0`` skips it). Rules that cannot be written as an aggregate
(ordering, per-row Python parsing, distributions) are returned as *fallback*
and run by Great Expectations on the same (persisted) frame.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["FUSED_EXPECTATIONS", "FusedRun", "compile_rule", "run_fused"]


def _F():
    from pyspark.sql import functions as F

    return F


def _col(name: str):
    return _F().col("`" + str(name).replace("`", "``") + "`")


def _mostly(kw: dict) -> float:
    m = kw.get("mostly")
    return 1.0 if m is None else float(m)


def _in_range(value: Any, kw: dict, lo_key: str = "min_value", hi_key: str = "max_value") -> bool:
    if value is None:
        return False
    lo, hi = kw.get(lo_key), kw.get(hi_key)
    try:
        if lo is not None and (value <= lo if kw.get("strict_min") else value < lo):
            return False
        if hi is not None and (value >= hi if kw.get("strict_max") else value > hi):
            return False
    except TypeError:
        return False
    return True


def _outside(c, kw: dict):
    """Row predicate: ``c`` falls outside [min_value, max_value] (strict_* honoured)."""
    F = _F()
    lo, hi = kw.get("min_value"), kw.get("max_value")
    cond = F.lit(False)
    if lo is not None:
        cond = cond | ((c <= F.lit(lo)) if kw.get("strict_min") else (c < F.lit(lo)))
    if hi is not None:
        cond = cond | ((c >= F.lit(hi)) if kw.get("strict_max") else (c > F.lit(hi)))
    return cond


# ── rule plans ───────────────────────────────────────────────────────────────

@dataclass
class _Plan:
    """What one rule adds to the fused query, and how to read its result back."""

    aggs: dict[str, Any]                                   # suffix → aggregate Column
    finish: Callable[[dict[str, Any], int], dict[str, Any]]
    sample: Any = None                                     # (bad-row predicate, value Column) or None
    schema_only: bool = False


def _map_plan(c, bad, kw: dict, *, nulls_count: bool = False, value=None) -> _Plan:
    """A per-row rule: count the rows that break it (nulls ignored unless ``nulls_count``)."""
    F = _F()
    aggs = {"bad": F.sum(F.when(bad, 1).otherwise(0)),
            "n": F.count(F.lit(1)) if nulls_count else F.count(c)}
    mostly = _mostly(kw)

    def finish(v: dict, _rows: int) -> dict:
        n, unexpected = int(v["n"] or 0), int(v["bad"] or 0)
        pct = 100.0 * unexpected / n if n else 0.0
        return {"success": (n == 0) or (1 - unexpected / n) >= mostly - 1e-12,
                "element_count": n, "unexpected_count": unexpected,
                "unexpected_pct": pct, "observed_value": None}

    return _Plan(aggs, finish, sample=(bad, c if value is None else value))


def _value_plan(expr, kw: dict, *, lo="min_value", hi="max_value", cast=None) -> _Plan:
    """An aggregate statistic that must fall in a range (min, max, mean, …)."""
    def finish(v: dict, _rows: int) -> dict:
        value = v["v"]
        if cast is not None and value is not None:
            value = cast(value)
        return {"success": _in_range(value, kw, lo, hi), "observed_value": value}

    return _Plan({"v": expr}, finish)


def _set_plan(c, kw: dict, check: Callable[[set, set], bool]) -> _Plan:
    F = _F()
    wanted = set(kw.get("value_set") or [])

    def finish(v: dict, _rows: int) -> dict:
        seen = set(v["v"] or [])
        return {"success": check(seen, wanted), "observed_value": sorted(seen, key=str)}

    return _Plan({"v": F.collect_set(c)}, finish)


def _duplicates_plan(key, count_rows, kw: dict) -> _Plan:
    """Uniqueness in one pass: rows minus distinct values = duplicated rows (mostly = 1 only)."""
    F = _F()

    def finish(v: dict, _rows: int) -> dict:
        n, distinct = int(v["n"] or 0), int(v["d"] or 0)
        extra = n - distinct
        return {"success": extra == 0, "element_count": n, "unexpected_count": extra,
                "unexpected_pct": 100.0 * extra / n if n else 0.0, "observed_value": None,
                "reason": f"{extra} duplicated row(s)" if extra else ""}

    return _Plan({"n": count_rows, "d": F.countDistinct(key)}, finish)


def _schema_plan(ok: Callable[[], tuple[bool, Any]]) -> _Plan:
    def finish(_v: dict, _rows: int) -> dict:
        success, observed = ok()
        return {"success": success, "observed_value": observed}

    return _Plan({}, finish, schema_only=True)


def _type_names(dtype) -> set[str]:
    names = {type(dtype).__name__.lower(), dtype.simpleString().lower(), dtype.typeName().lower()}
    return names | {n.replace("type", "") for n in names}


def compile_rule(df: Any, rule: dict) -> Optional[_Plan]:
    """The fused plan for one GE-shaped rule, or ``None`` when it needs Great Expectations."""
    F = _F()
    etype = str(rule.get("rule") or "")
    kw = {k: v for k, v in (rule.get("kwargs") or {}).items() if k not in ("meta", "batch_id")}
    name = kw.get("column") or rule.get("column")
    c = _col(name) if name else None
    columns = list(df.columns)

    # table shape: answered from the schema, no Spark job
    if etype == "expect_column_to_exist":
        return _schema_plan(lambda: (name in columns, None))
    if etype == "expect_table_columns_to_match_set":
        def match_set():
            want, have = set(kw.get("column_set") or []), set(columns)
            exact = kw.get("exact_match", True) is not False
            return (have == want if exact else want <= have), sorted(have)
        return _schema_plan(match_set)
    if etype == "expect_table_columns_to_match_ordered_list":
        return _schema_plan(lambda: (columns == list(kw.get("column_list") or []), columns))
    if etype == "expect_table_column_count_to_equal":
        return _schema_plan(lambda: (len(columns) == kw.get("value"), len(columns)))
    if etype == "expect_table_column_count_to_be_between":
        return _schema_plan(lambda: (_in_range(len(columns), kw), len(columns)))
    if etype in ("expect_column_values_to_be_of_type", "expect_column_values_to_be_in_type_list"):
        wanted = [kw.get("type_")] if etype.endswith("of_type") else list(kw.get("type_list") or [])
        dtype = df.schema[name].dataType

        def type_ok():
            names = _type_names(dtype)
            return any(str(t).lower() in names for t in wanted if t), type(dtype).__name__
        return _schema_plan(type_ok)

    # table level
    if etype == "expect_table_row_count_to_be_between":
        return _value_plan(F.count(F.lit(1)), kw, cast=int)
    if etype == "expect_table_row_count_to_equal":
        return _value_plan(F.count(F.lit(1)), {"min_value": kw.get("value"),
                                               "max_value": kw.get("value")}, cast=int)

    # per-row rules
    notnull = c.isNotNull() if c is not None else None
    if etype == "expect_column_values_to_not_be_null":
        return _map_plan(c, c.isNull(), kw, nulls_count=True)
    if etype == "expect_column_values_to_be_null":
        return _map_plan(c, notnull, kw, nulls_count=True)
    if etype == "expect_column_values_to_be_in_set":
        return _map_plan(c, notnull & ~c.isin(list(kw.get("value_set") or [])), kw)
    if etype == "expect_column_values_to_not_be_in_set":
        return _map_plan(c, notnull & c.isin(list(kw.get("value_set") or [])), kw)
    if etype == "expect_column_values_to_be_between":
        return _map_plan(c, notnull & _outside(c, kw), kw)
    if etype == "expect_column_value_lengths_to_be_between":
        return _map_plan(c, notnull & _outside(F.length(c.cast("string")), kw), kw)
    if etype == "expect_column_value_lengths_to_equal":
        return _map_plan(c, notnull & (F.length(c.cast("string")) != F.lit(kw.get("value"))), kw)
    if etype == "expect_column_values_to_match_regex":
        return _map_plan(c, notnull & ~c.cast("string").rlike(str(kw.get("regex"))), kw)
    if etype == "expect_column_values_to_not_match_regex":
        return _map_plan(c, notnull & c.cast("string").rlike(str(kw.get("regex"))), kw)
    if etype in ("expect_column_values_to_match_regex_list",
                 "expect_column_values_to_not_match_regex_list"):
        hits = [c.cast("string").rlike(str(p)) for p in kw.get("regex_list") or []]
        if not hits:
            return None
        anyhit, allhit = hits[0], hits[0]
        for h in hits[1:]:
            anyhit, allhit = anyhit | h, allhit & h
        if etype.endswith("not_match_regex_list"):
            return _map_plan(c, notnull & anyhit, kw)
        ok = allhit if kw.get("match_on") == "all" else anyhit
        return _map_plan(c, notnull & ~ok, kw)
    if etype == "expect_column_values_to_be_unique":
        if _mostly(kw) < 1:
            return None
        return _duplicates_plan(c, F.count(c), kw)
    if etype == "expect_compound_columns_to_be_unique":
        cols = [_col(x) for x in kw.get("column_list") or []]
        if not cols or _mostly(kw) < 1:
            return None
        any_value = cols[0].isNotNull()
        for x in cols[1:]:
            any_value = any_value | x.isNotNull()
        return _duplicates_plan(F.when(any_value, F.struct(*cols)),
                                F.sum(F.when(any_value, 1).otherwise(0)), kw)
    if etype == "expect_select_column_values_to_be_unique_within_record":
        cols = [_col(x) for x in kw.get("column_list") or []]
        clash = F.lit(False)
        for i, a in enumerate(cols):
            for b in cols[i + 1:]:
                clash = clash | (a.isNotNull() & b.isNotNull() & (a == b))
        return _map_plan(cols[0], clash, kw, nulls_count=True, value=F.to_json(F.struct(*cols)))
    if etype in ("expect_column_pair_values_to_be_equal",
                 "expect_column_pair_values_a_to_be_greater_than_b"):
        a, b = _col(kw.get("column_A")), _col(kw.get("column_B"))
        if etype.endswith("equal"):
            ok = a == b
        else:
            ok = (a >= b) if kw.get("or_equal") else (a > b)
        considered = a.isNotNull() | b.isNotNull()
        pair = F.to_json(F.struct(a.alias("A"), b.alias("B")))
        plan = _map_plan(a, considered & ~F.coalesce(ok, F.lit(False)), kw, nulls_count=True, value=pair)
        plan.aggs["n"] = F.sum(F.when(considered, 1).otherwise(0))
        return plan
    if etype == "expect_column_pair_values_to_be_in_set":
        a, b = _col(kw.get("column_A")), _col(kw.get("column_B"))
        pairs = [tuple(p) for p in kw.get("value_pairs_set") or []]
        allowed = F.lit(False)
        for x, y in pairs:
            allowed = allowed | ((a == F.lit(x)) & (b == F.lit(y)))
        considered = a.isNotNull() | b.isNotNull()
        pair = F.to_json(F.struct(a.alias("A"), b.alias("B")))
        plan = _map_plan(a, considered & ~F.coalesce(allowed, F.lit(False)), kw, nulls_count=True,
                         value=pair)
        plan.aggs["n"] = F.sum(F.when(considered, 1).otherwise(0))
        return plan
    if etype == "expect_multicolumn_sum_to_equal":
        cols = [_col(x) for x in kw.get("column_list") or []]
        if not cols:
            return None
        total = cols[0]
        for x in cols[1:]:
            total = total + x
        return _map_plan(cols[0], F.coalesce(total != F.lit(kw.get("sum_total")), F.lit(True)), kw,
                         nulls_count=True, value=F.to_json(F.struct(*cols)))

    # column statistics
    stats = {
        "expect_column_min_to_be_between": F.min,
        "expect_column_max_to_be_between": F.max,
        "expect_column_mean_to_be_between": F.avg,
        "expect_column_stdev_to_be_between": F.stddev_samp,
        "expect_column_sum_to_be_between": F.sum,
    }
    if etype in stats:
        return _value_plan(stats[etype](c), kw)
    if etype == "expect_column_median_to_be_between":
        return _value_plan(F.expr(f"percentile({_quoted(name)}, 0.5)"), kw)
    if etype == "expect_column_unique_value_count_to_be_between":
        return _value_plan(F.countDistinct(c), kw, cast=int)
    if etype == "expect_column_proportion_of_unique_values_to_be_between":
        return _value_plan(F.countDistinct(c) / F.count(c), kw)
    # Quantiles are left to Great Expectations: its value depends on the interpolation
    # method (pandas "nearest" rank), which needs the row count before the pass.
    if etype == "expect_column_distinct_values_to_be_in_set":
        return _set_plan(c, kw, lambda seen, want: seen <= want)
    if etype == "expect_column_distinct_values_to_contain_set":
        return _set_plan(c, kw, lambda seen, want: want <= seen)
    if etype == "expect_column_distinct_values_to_equal_set":
        return _set_plan(c, kw, lambda seen, want: seen == want)
    return None


def _quoted(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


FUSED_EXPECTATIONS = (
    "expect_column_to_exist", "expect_table_columns_to_match_set",
    "expect_table_columns_to_match_ordered_list", "expect_table_column_count_to_equal",
    "expect_table_column_count_to_be_between", "expect_column_values_to_be_of_type",
    "expect_column_values_to_be_in_type_list", "expect_table_row_count_to_be_between",
    "expect_table_row_count_to_equal", "expect_column_values_to_not_be_null",
    "expect_column_values_to_be_null", "expect_column_values_to_be_in_set",
    "expect_column_values_to_not_be_in_set", "expect_column_values_to_be_between",
    "expect_column_value_lengths_to_be_between", "expect_column_value_lengths_to_equal",
    "expect_column_values_to_match_regex", "expect_column_values_to_not_match_regex",
    "expect_column_values_to_match_regex_list", "expect_column_values_to_not_match_regex_list",
    "expect_column_values_to_be_unique", "expect_compound_columns_to_be_unique",
    "expect_select_column_values_to_be_unique_within_record",
    "expect_column_pair_values_to_be_equal", "expect_column_pair_values_a_to_be_greater_than_b",
    "expect_column_pair_values_to_be_in_set",
    "expect_multicolumn_sum_to_equal", "expect_column_min_to_be_between",
    "expect_column_max_to_be_between", "expect_column_mean_to_be_between",
    "expect_column_median_to_be_between", "expect_column_stdev_to_be_between",
    "expect_column_sum_to_be_between", "expect_column_unique_value_count_to_be_between",
    "expect_column_proportion_of_unique_values_to_be_between",
    "expect_column_distinct_values_to_be_in_set",
    "expect_column_distinct_values_to_contain_set", "expect_column_distinct_values_to_equal_set",
)


# ── running ──────────────────────────────────────────────────────────────────

@dataclass
class FusedRun:
    """Result of ``run_fused``: report rows (GE row shape) plus what was left to GE."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    fallback: list[dict[str, Any]] = field(default_factory=list)
    row_count: Optional[int] = None
    fused: int = 0
    spark_jobs: int = 0          # jobs this module started (1 aggregate + 1 sample job at most)


def _row(rule: dict, result: dict) -> dict:
    return {"rule": rule.get("rule"), "column": rule.get("column"),
            "kwargs": dict(rule.get("kwargs") or {}), "meta": dict(rule.get("meta") or {}),
            "partial_unexpected": [], "reason": "", **result}


def run_fused(df: Any, rules: list[dict], *, samples: int = 5) -> FusedRun:
    """Validate GE-shaped ``rules`` on a Spark DataFrame with one aggregate job.

    Returns a ``FusedRun``. ``rows`` are in the row shape Great Expectations
    reports (``success``, ``element_count``, ``unexpected_count``,
    ``observed_value``, ``partial_unexpected``, the rule's ``meta``);
    ``fallback`` lists the rules the caller must run another way.
    """
    F = _F()
    out = FusedRun()
    plans: list[tuple[dict, _Plan, str]] = []
    for i, rule in enumerate(rules):
        try:
            plan = compile_rule(df, rule)
            if plan is not None and plan.aggs:
                df.agg(*[e.alias(f"r{i}__{k}") for k, e in plan.aggs.items()])   # analysis only, no job
        except Exception as exc:  # noqa: BLE001 — e.g. a type the expression cannot take
            logger.debug("fused: %s falls back to Great Expectations: %s", rule.get("rule"), exc)
            plan = None
        if plan is None:
            out.fallback.append(rule)
        else:
            plans.append((rule, plan, f"r{i}"))
    if not plans:
        return out

    exprs = [F.count(F.lit(1)).alias("__rows")]
    for _rule, plan, prefix in plans:
        exprs += [e.alias(f"{prefix}__{k}") for k, e in plan.aggs.items()]
    values: dict[str, Any] = {}
    if any(not plan.schema_only for _r, plan, _p in plans):
        values = df.agg(*exprs).collect()[0].asDict()           # the one pass over the data
        out.spark_jobs = 1
        out.row_count = int(values["__rows"])

    failed_samples = []
    for rule, plan, prefix in plans:
        mine = {k: values.get(f"{prefix}__{k}") for k in plan.aggs}
        result = _row(rule, plan.finish(mine, out.row_count or 0))
        out.rows.append(result)
        if not result["success"] and plan.sample is not None and samples > 0:
            failed_samples.append((len(out.rows) - 1, plan.sample))
    out.fused = len(plans)

    if failed_samples:
        branches = [df.where(bad).select(F.lit(idx).alias("r"), value.cast("string").alias("v"))
                    .limit(samples) for idx, (bad, value) in failed_samples]
        union = branches[0]
        for b in branches[1:]:
            union = union.unionByName(b)
        for rec in union.collect():
            out.rows[rec["r"]]["partial_unexpected"].append(rec["v"])
        out.spark_jobs += 1
    return out
