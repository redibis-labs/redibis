"""
redibis.quality.authoring — author quality rules from code (pandas or Spark).

Point it at a DataFrame and it discovers quality rules on the *whole* frame with
Great Expectations, validates them, and returns a draft you can curate in code.
Save the draft as a quality **run** (the same run bucket ``redibis quality-run`` reads)
and review / diff / edit / merge it from the CLI — or do the whole cycle in code::

    from redibis.quality.authoring import QualityAuthor

    qa = QualityAuthor("sales.transactions", output_dir="./reports")
    draft = qa.author(spark_df)              # or a pandas DataFrame
    draft.drop(columns=["notes"], rule_types=["in_set"])
    draft.add_sql("SELECT * FROM ${object} WHERE amount < 0",
                  description="no negative amounts")   # your own SQL rule
    run = qa.save(draft, note="partition 2026-09-25")
    print(qa.diff(run.run_id))               # what a merge would change
    qa.merge(run.run_id)                     # → active contract (single writer)

A draft can also travel as a file (``draft.to_yaml(path)``) when the notebook
cannot reach the contract store; ``redibis quality-run import`` turns it into a run.

Merge semantics (``ContractStore.upsert``): every column present in the run gets
its quality rules **replaced** by the run's rules; columns absent from the run
keep their current rules.
"""

from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

from redibis.contracts.type_inference import dtype_map_from_dataframe, is_spark_dataframe
from redibis.quality.sql_rules import is_sql_rule, sql_rule, sql_rule_to_contract

DRAFT_FORMAT = "redibis.io/quality-draft/v1"
TABLE_LEVEL = "(table)"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(obj: Any) -> Any:
    """Deep-copy into plain JSON types (GE hands back str subclasses / numpy scalars)."""
    def default(o: Any) -> Any:
        if hasattr(o, "item"):
            return o.item()
        return str(o)
    return json.loads(json.dumps(obj, default=default))


def _sorted_if_possible(values: list) -> list:
    try:
        return sorted(values)
    except TypeError:
        return values


def _canonicalize(payload: dict) -> dict:
    """Sort unordered value lists so identical rules hash identically across runs.

    Great Expectations returns value sets and column sets in arbitrary order; left
    as-is every refresh would show the same rule as removed + added.
    """
    for _column, qlist, pos in _iter_rule_slots(payload):
        q = qlist[pos]
        if not isinstance(q, dict):
            continue
        args = q.get("arguments")
        if isinstance(args, dict) and isinstance(args.get("validValues"), list):
            args["validValues"] = _sorted_if_possible(args["validValues"])
        impl = q.get("implementation")
        kwargs = (impl or {}).get("kwargs")
        if isinstance(kwargs, dict):
            for key in ("value_set", "column_set"):
                if isinstance(kwargs.get(key), list):
                    kwargs[key] = _sorted_if_possible(kwargs[key])
            impl["kwargs"] = dict(sorted(kwargs.items()))   # same rule → same text, run to run
    return payload


def _split_table(table: str) -> tuple[str, str]:
    if "." in table:
        db, tbl = table.split(".", 1)
        return db, tbl
    return "", table


# ─────────────────────────────────────────────────────────────────────────────
# Rules inside an ODCS quality partial
# ─────────────────────────────────────────────────────────────────────────────

def _iter_rule_slots(payload: dict) -> Iterable[tuple[Optional[str], list, int]]:
    """Yield ``(column, quality_list, position)`` in ``extract_rules`` order."""
    for schema_obj in payload.get("schema", []) or []:
        table_q = schema_obj.get("quality", []) or []
        for i in range(len(table_q)):
            yield None, table_q, i
        for prop in schema_obj.get("properties", []) or []:
            col_q = prop.get("quality", []) or []
            for i in range(len(col_q)):
                yield prop.get("name"), col_q, i


# Friendly names people type → canonical rule type (``extract_rules``).
_RULE_ALIASES = {
    "in_set": "set", "values": "set", "valid_values": "set", "validvalues": "set",
    "pattern": "regex", "format": "regex",
    "null": "not_null", "missing": "not_null", "required": "not_null",
    "duplicates": "unique", "rows": "row_count", "rowcount": "row_count",
}


# Great Expectations names of the rules stored in portable ODCS form, so
# ``rule_types=["expect_column_values_to_be_in_set"]`` finds a ``validValues`` rule.
_NATIVE_GE_NAMES = {
    "not_null": ("expect_column_values_to_not_be_null", "expect_column_values_to_be_null"),
    "unique": ("expect_column_values_to_be_unique",),
    "set": ("expect_column_values_to_be_in_set",),
    "regex": ("expect_column_values_to_match_regex",),
    "unique_count": ("expect_column_unique_value_count_to_be_between",),
    "row_count": ("expect_table_row_count_to_be_between", "expect_table_row_count_to_equal"),
}


def _rule_names(rule: dict) -> set[str]:
    """Every name a listed rule answers to (canonical, ODCS, Great Expectations)."""
    names = {rule.get("type"), rule.get("odcs_rule"), rule.get("expectation_type")}
    names.update(_NATIVE_GE_NAMES.get(rule.get("type") or "", ()))
    return {str(n).lower() for n in names if n}


def _expectation_type(raw: Any) -> str:
    if isinstance(raw, dict):
        impl = raw.get("implementation")
        if isinstance(impl, dict):
            return str(impl.get("expectation_type") or "")
    return ""


def list_rules(payload: dict) -> list[dict]:
    """Numbered rules of a quality partial — the numbering ``quality-run show`` prints.

    Each item: ``{index, column, type, expectation_type, params, id}``. ``column``
    is ``None`` for table-level rules.
    """
    from redibis.contracts.rules import extract_rules

    canonical = extract_rules(payload)
    out: list[dict] = []
    for index, ((column, qlist, pos), rule) in enumerate(
        zip(_iter_rule_slots(payload), canonical)
    ):
        raw = qlist[pos]
        params = dict(rule.params or {})
        etype = _expectation_type(raw)
        if etype:
            kwargs = dict((raw.get("implementation") or {}).get("kwargs") or {})
            kwargs.pop("column", None)
            params = kwargs
        out.append({
            "index": index,
            "column": column,
            "type": rule.type,
            "odcs_rule": str(raw.get("rule") or "") if isinstance(raw, dict) else "",
            "expectation_type": etype,
            "params": params,
            "id": rule.rule_id,
            "severity": rule.severity,
        })
    return out


def rule_label(rule: dict) -> str:
    """Short human label: ``not_null`` or ``expect_column_value_lengths_to_be_between {…}``."""
    name = rule.get("expectation_type") or rule.get("type") or "?"
    params = rule.get("params") or {}
    return f"{name} {params}" if params else name


def drop_rules(
    payload: dict,
    *,
    columns: Iterable[str] = (),
    rule_types: Iterable[str] = (),
    indices: Iterable[int] = (),
) -> tuple[dict, list[dict]]:
    """Return ``(new_payload, removed_rules)``. The input payload is not mutated.

    ``rule_types`` matches, case-insensitively, the canonical type (``set``,
    ``regex``, ``not_null``…), a friendly alias (``in_set``, ``pattern``…), the
    ODCS rule name (``validValues``, ``missingCount``…) or the Great Expectations
    name (``expect_column_values_to_be_in_set``).
    """
    cols = {str(c) for c in columns}
    types = {str(t).lower() for t in rule_types}
    types |= {_RULE_ALIASES[t] for t in types if t in _RULE_ALIASES}
    idx = {int(i) for i in indices}
    new_payload = copy.deepcopy(payload)
    rules = list_rules(new_payload)
    doomed = {
        r["index"] for r in rules
        if r["index"] in idx
        or (r["column"] is not None and r["column"] in cols)
        or bool(_rule_names(r) & types)
    }
    removed = [r for r in rules if r["index"] in doomed]
    # Delete back-to-front so earlier positions stay valid.
    slots = list(_iter_rule_slots(new_payload))
    for index in sorted(doomed, reverse=True):
        _column, qlist, pos = slots[index]
        del qlist[pos]
    return new_payload, removed


SEVERITIES = ("P1", "P2", "P3")

# Great Expectations defaults ``to_code`` leaves out, and the order it writes arguments in.
_GE_DEFAULTS = {"mostly": 1.0, "strict_min": False, "strict_max": False}
_ARG_ORDER = {k: i for i, k in enumerate(
    ("column_list", "column_set", "value_set", "regex", "regex_list", "min_value", "max_value"))}

# Table name used when rules are written for a DataFrame that has no table yet.
ADHOC_TABLE = "adhoc.dataframe"
_ANY: Any = object()


def set_severity(
    payload: dict,
    severity: str,
    *,
    columns: Iterable[str] = (),
    rule_types: Iterable[str] = (),
    indices: Iterable[int] = (),
) -> tuple[dict, list[dict]]:
    """Set ``severity`` on the matching rules; returns ``(new_payload, changed_rules)``.

    Unlike ``drop_rules`` the criteria combine: ``columns=["email"],
    rule_types=["regex"]`` means *regex rules on email*. ``columns=[None]``
    selects table-level rules. With no criteria every rule matches. Severity
    travels in the contract; monitors and pipeline gates read it (``P1`` usually
    blocks, ``P2``/``P3`` warn).
    """
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    cols = set(columns)
    types = {str(x).lower() for x in rule_types}
    types |= {_RULE_ALIASES[x] for x in types if x in _RULE_ALIASES}
    idx = {int(i) for i in indices}
    new_payload = copy.deepcopy(payload)
    changed = []
    for rule, (_column, qlist, pos) in zip(list_rules(new_payload), _iter_rule_slots(new_payload)):
        if idx and rule["index"] not in idx:
            continue
        if cols and rule["column"] not in cols:
            continue
        if types and not _rule_names(rule) & types:
            continue
        if isinstance(qlist[pos], dict):
            qlist[pos]["severity"] = severity
            changed.append({**rule, "severity": severity})
    return new_payload, changed


def _env_flag(name: str) -> bool:
    import os
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _pii_withheld(active: Optional[dict]) -> dict[str, str]:
    """Columns whose rules the merge's privacy guard will not store.

    Mirrors ``ContractStore.upsert(strip_pii_quality=True)``: on a column the
    contract marks as personal data, rules that embed real values are removed —
    all rules by default, only value-bearing ones with
    ``REDIBIS_PII_QUALITY_KEEP_SAFE=1``. Returns ``{column: "all" | "value"}``.
    """
    if not active or _env_flag("REDIBIS_KEEP_QUALITY_ON_PII"):
        return {}
    from redibis.contracts.privacy import column_is_pii

    mode = "value" if _env_flag("REDIBIS_PII_QUALITY_KEEP_SAFE") else "all"
    out: dict[str, str] = {}
    for schema_obj in active.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict) and prop.get("name") and column_is_pii(prop):
                out[str(prop["name"])] = mode
    return out


def diff_rules(active: Optional[dict], payload: dict) -> list[dict]:
    """What merging ``payload`` into ``active`` changes, per affected column.

    Only columns (and the table level) that the run carries are affected — the
    merge replaces their rules. On personal-data columns the merge's privacy
    guard keeps value-bearing rules out of the contract; those are reported as
    ``withheld`` rather than ``added``. Returns
    ``[{column, added: [rule], removed: [rule], kept: [rule], withheld: [rule]}]``.
    """
    from redibis.contracts.privacy import quality_rule_is_value_bearing

    guarded = _pii_withheld(active)
    withheld_by_column: dict[Optional[str], list[dict]] = {}
    withheld_ids: set[tuple[Optional[str], str]] = set()
    for rule, (column, qlist, pos) in zip(list_rules(payload), _iter_rule_slots(payload)):
        mode = guarded.get(column) if column else None
        if mode and (mode == "all" or quality_rule_is_value_bearing(qlist[pos])):
            withheld_by_column.setdefault(column, []).append(rule)
            withheld_ids.add((column, rule["id"]))
    def by_column(contract: Optional[dict]) -> dict[Optional[str], dict[str, dict]]:
        out: dict[Optional[str], dict[str, dict]] = {}
        if not contract:
            return out
        for r in list_rules(contract):
            out.setdefault(r["column"], {})[r["id"]] = r
        return out

    new = by_column(payload)
    old = by_column(active)
    affected: list[Optional[str]] = []
    for schema_obj in payload.get("schema", []) or []:
        if schema_obj.get("quality"):
            affected.append(None)
        for prop in schema_obj.get("properties", []) or []:
            affected.append(prop.get("name"))
    changes = []
    for column in affected:
        n = {k: v for k, v in new.get(column, {}).items() if (column, k) not in withheld_ids}
        o = old.get(column, {})
        withheld = withheld_by_column.get(column, [])
        changes.append({
            "column": column,
            "added": [n[k] for k in n if k not in o],
            "removed": [o[k] for k in o if k not in n],
            "kept": [n[k] for k in n if k in o],
            "withheld": withheld,
        })
    return changes


def format_diff(changes: list[dict]) -> str:
    lines: list[str] = []
    added = sum(len(c["added"]) for c in changes)
    removed = sum(len(c["removed"]) for c in changes)
    withheld = sum(len(c.get("withheld") or []) for c in changes)
    for c in changes:
        if not c["added"] and not c["removed"] and not c.get("withheld"):
            continue
        lines.append(f"{c['column'] or TABLE_LEVEL}:")
        lines += [f"  + {rule_label(r)}" for r in c["added"]]
        lines += [f"  - {rule_label(r)}" for r in c["removed"]]
        lines += [f"  ~ {rule_label(r)}   (not stored: personal-data column)"
                  for r in c.get("withheld") or []]
    if not added and not removed:
        note = (f" ({withheld} rule(s) on personal-data columns are never stored)"
                if withheld else "")
        return ("No rule changes: merging this run would leave the contract's rules "
                "as they are." + note)
    touched = sum(1 for c in changes if c["added"] or c["removed"])
    lines.append(f"{added} added, {removed} removed "
                 f"(across {touched} column{'' if touched == 1 else 's'})"
                 + (f"; {withheld} withheld on personal-data columns" if withheld else ""))
    return "\n".join(lines)


# Rules Great Expectations writes as an exact snapshot of the profiled data
# (mean == 2501.43…). They rarely hold on the next partition: drop or relax them.
SNAPSHOT_RULES = (
    "row_count",
    "unique_count",
    "expect_column_min_to_be_between",
    "expect_column_max_to_be_between",
    "expect_column_mean_to_be_between",
    "expect_column_median_to_be_between",
    "expect_column_stdev_to_be_between",
    "expect_column_quantile_values_to_be_between",
    "expect_column_proportion_of_unique_values_to_be_between",
)

_PROPORTION_TYPES = ("proportion", "_percent")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _widen(lo: Any, hi: Any, tolerance: float, *, integer: bool = False,
           floor: Optional[float] = None, ceil: Optional[float] = None) -> tuple[Any, Any]:
    """Widen ``[lo, hi]`` by ``tolerance`` of its span (or of its size for a point).

    A range never changes sign (``[0, 5000]`` stays non-negative), and float
    bounds are rounded outward to a clean step so two partitions with nearly the
    same range produce identical rules (no diff churn from ``5499.978`` vs ``5500``).
    """
    import math

    span = hi - lo
    pad = tolerance * (span if span > 0 else max(abs(lo), abs(hi))) or tolerance
    new_lo, new_hi = lo - pad, hi + pad
    if lo >= 0:
        new_lo = max(0, new_lo)
    if hi <= 0:
        new_hi = min(0, new_hi)
    if not integer:
        step = 10 ** math.floor(math.log10(pad))
        eps = 1e-9  # guard against 0.945 / 0.001 == 944.9999999
        new_lo = float(math.floor(new_lo / step + eps) * step)
        new_hi = float(math.ceil(new_hi / step - eps) * step)
    if floor is not None:
        new_lo = max(floor, new_lo)
    if ceil is not None:
        new_hi = min(ceil, new_hi)
    if integer:
        return int(math.floor(new_lo)), int(math.ceil(new_hi))
    return round(new_lo, 10), round(new_hi, 10)


def relax_rules(payload: dict, tolerance: float = 0.1) -> tuple[dict, int]:
    """Widen numeric ranges by ``tolerance`` (0.1 = ±10 %). Returns ``(payload, n)``.

    Touches GE ``min_value``/``max_value`` and quantile ranges, and ODCS
    ``mustBeBetween``. Dates, strings, value sets, regexes and value lengths are
    left alone — those are structural, not statistical.
    """
    if tolerance <= 0:
        return copy.deepcopy(payload), 0
    new_payload = copy.deepcopy(payload)
    changed = 0
    for _column, qlist, pos in _iter_rule_slots(new_payload):
        q = qlist[pos]
        if not isinstance(q, dict) or q.get("type") == "sql":
            continue   # SQL thresholds are business limits, not measured ranges
        between = q.get("mustBeBetween")
        if (isinstance(between, list) and len(between) == 2
                and all(_is_number(v) for v in between)):
            integer = all(isinstance(v, int) for v in between)
            q["mustBeBetween"] = list(_widen(between[0], between[1], tolerance,
                                             integer=integer, floor=0 if integer else None))
            changed += 1
        impl = q.get("implementation")
        if not isinstance(impl, dict):
            continue
        etype = str(impl.get("expectation_type") or "")
        if "value_lengths" in etype:
            continue
        kwargs = impl.get("kwargs") or {}
        bounded = (0.0, 1.0) if any(t in etype for t in _PROPORTION_TYPES) else (None, None)
        lo, hi = kwargs.get("min_value"), kwargs.get("max_value")
        if _is_number(lo) and _is_number(hi):
            integer = isinstance(lo, int) and isinstance(hi, int)
            kwargs["min_value"], kwargs["max_value"] = _widen(
                lo, hi, tolerance, integer=integer, floor=bounded[0], ceil=bounded[1])
            changed += 1
        ranges = (kwargs.get("quantile_ranges") or {}).get("value_ranges")
        if isinstance(ranges, list):
            kwargs["quantile_ranges"]["value_ranges"] = [
                list(_widen(r[0], r[1], tolerance)) if (
                    isinstance(r, list) and len(r) == 2 and all(_is_number(v) for v in r)
                ) else r
                for r in ranges
            ]
            changed += 1
    return new_payload, changed


def _attach_sql(payload: dict, rules: Iterable[dict], table: str) -> list[dict]:
    """Append RULES-shape SQL rules to ``payload`` (table level, or their column).

    Returns the ODCS entries added. Creates the schema object / property when
    the payload has none yet (a program made only of SQL rules).
    """
    _db, tbl = _split_table(table)
    schema = payload.setdefault("schema", [])
    if not schema:
        schema.append({"name": tbl, "properties": []})
    target = schema[0]
    added: list[dict] = []
    for r in rules:
        entry = sql_rule_to_contract(r)
        column = r.get("column")
        if column:
            props = target.setdefault("properties", [])
            prop = next((p for p in props if p.get("name") == column), None)
            if prop is None:
                prop = {"name": column}
                props.append(prop)
            qlist = prop.setdefault("quality", [])
        else:
            qlist = target.setdefault("quality", [])
        if entry not in qlist:
            qlist.append(entry)
            added.append(entry)
    return added


def _known_expectations() -> Optional[set[str]]:
    try:
        import great_expectations.expectations.core  # noqa: F401 — registers the core set
        from great_expectations.expectations.registry import _registered_expectations
    except Exception:  # noqa: BLE001 — without Great Expectations, names are checked at run time
        return None
    return set(_registered_expectations)


def _check_expectation_name(name: str) -> None:
    """Fail early, with a suggestion, on a misspelt expectation name."""
    import difflib

    if not isinstance(name, str) or not name.startswith("expect_"):
        raise ValueError(f"{name!r} is not an expectation name (they start with 'expect_')")
    known = _known_expectations()
    if known and name not in known:
        close = difflib.get_close_matches(name, sorted(known), n=1)
        hint = f" — did you mean {close[0]!r}?" if close else ""
        raise ValueError(f"unknown expectation {name!r}{hint}")


def _merge_payload(target: dict, extra: dict) -> list[dict]:
    """Append ``extra``'s quality rules to ``target`` (same table); returns the entries added.

    Table-level rules go to the first schema object; column rules go to the
    property of the same name (created when missing). Identical entries are
    not duplicated.
    """
    schema = target.setdefault("schema", [])
    extra_schema = extra.get("schema", []) or []
    if not schema:
        schema.append({"name": (extra_schema[0].get("name") if extra_schema else ""), "properties": []})
    dest = schema[0]
    added: list[dict] = []
    for obj in extra_schema:
        for q in obj.get("quality", []) or []:
            qlist = dest.setdefault("quality", [])
            if q not in qlist:
                qlist.append(q)
                added.append(q)
        for prop in obj.get("properties", []) or []:
            props = dest.setdefault("properties", [])
            mine = next((p for p in props if p.get("name") == prop.get("name")), None)
            if mine is None:
                mine = {k: v for k, v in prop.items() if k != "quality"}
                props.append(mine)
            qlist = mine.setdefault("quality", [])
            for q in prop.get("quality", []) or []:
                if q not in qlist:
                    qlist.append(q)
                    added.append(q)
    return added


def _covered_columns(payload: dict) -> set[str]:
    return {prop.get("name") for schema_obj in payload.get("schema", []) or []
            for prop in schema_obj.get("properties", []) or [] if prop.get("quality")}


def _column_stats(df: Any, columns: list[str], spark: bool) -> tuple[int, dict[str, dict]]:
    """Row count + per-column nulls / distinct / string lengths, over the whole frame."""
    stats: dict[str, dict] = {}
    if spark:
        from pyspark.sql import functions as F

        string_cols = {f.name for f in df.schema.fields
                       if f.dataType.simpleString() == "string"}
        aggs = [F.count(F.lit(1)).alias("__rows")]
        for i, c in enumerate(columns):
            col = F.col(f"`{c}`")
            aggs += [F.sum(col.isNull().cast("long")).alias(f"n{i}"),
                     F.countDistinct(col).alias(f"d{i}")]
            if c in string_cols:
                aggs += [F.min(F.length(col)).alias(f"lo{i}"),
                         F.max(F.length(col)).alias(f"hi{i}")]
        row = df.agg(*aggs).collect()[0].asDict()
        total = int(row["__rows"])
        for i, c in enumerate(columns):
            stats[c] = {"nulls": int(row[f"n{i}"] or 0), "distinct": int(row[f"d{i}"] or 0),
                        "min_len": row.get(f"lo{i}"), "max_len": row.get(f"hi{i}")}
        return total, stats
    import pandas as pd

    total = len(df)
    for c in columns:
        s = df[c]
        entry = {"nulls": int(s.isna().sum()), "distinct": int(s.nunique(dropna=True)),
                 "min_len": None, "max_len": None}
        if s.dtype == object or pd.api.types.is_string_dtype(s.dtype):
            lengths = s.dropna().astype(str).str.len()
            if len(lengths):
                entry["min_len"], entry["max_len"] = int(lengths.min()), int(lengths.max())
        stats[c] = entry
    return total, stats


def ensure_column_coverage(df: Any, payload: dict, *, spark: bool) -> tuple[dict, list[str], int]:
    """Give columns the profiler skipped baseline rules computed on the whole frame.

    Great Expectations' onboarding assistant can skip high-cardinality text
    columns — usually the keys, where "never null, always unique" matters most.
    Returns ``(payload, covered_columns, row_count)``; ``row_count`` is ``-1``
    when nothing needed covering (no extra pass over the data).
    """
    from redibis.contracts.type_inference import infer_types_from_dtype

    missing = [str(c) for c in df.columns if str(c) not in _covered_columns(payload)]
    if not missing:
        return payload, [], -1
    total, stats = _column_stats(df, missing, spark)
    dtypes = dtype_map_from_dataframe(df)
    new_payload = copy.deepcopy(payload)
    schema = new_payload.setdefault("schema", [])
    if not schema:
        schema.append({"properties": []})
    props = schema[0].setdefault("properties", [])
    by_name = {p.get("name"): p for p in props}
    note = "added by redibis: column not covered by the profiler"
    covered: list[str] = []
    for c in missing:
        st = stats[c]
        rules: list[dict] = []
        if total and st["nulls"] == 0:
            rules.append({"rule": "missingCount", "mustBe": 0, "description": note})
        if total and st["nulls"] == 0 and st["distinct"] == total:
            rules.append({"rule": "duplicateCount", "mustBe": 0, "description": note})
        if st["min_len"] is not None and st["max_len"] is not None:
            rules.append({"engine": "greatExpectations", "description": note,
                          "implementation": {
                              "expectation_type": "expect_column_value_lengths_to_be_between",
                              "kwargs": {"column": c, "min_value": int(st["min_len"]),
                                         "max_value": int(st["max_len"]), "mostly": 1.0}}})
        if not rules:
            continue
        prop = by_name.get(c)
        if prop is None:
            physical, logical = infer_types_from_dtype(dtypes.get(c, "string"))
            prop = {"name": c, "physicalType": physical, "logicalType": logical}
            props.append(prop)
        if st["nulls"] == 0:
            prop["required"] = True
        if st["nulls"] == 0 and st["distinct"] == total:
            prop["unique"] = True
        prop["quality"] = rules
        covered.append(c)
    props.sort(key=lambda p: str(p.get("name")))
    return new_payload, covered, total


# ─────────────────────────────────────────────────────────────────────────────
# Draft
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QualityDraft:
    """Quality rules discovered from one DataFrame, not yet in any contract."""

    table: str
    payload: dict
    passed: int = 0
    total: int = 0
    engine: str = "pandas"
    rows: Optional[int] = None
    created_at: str = field(default_factory=_utc_now_iso)
    notes: list[str] = field(default_factory=list)

    @property
    def rules(self) -> list[dict]:
        return list_rules(self.payload)

    def drop(
        self,
        *,
        columns: Iterable[str] = (),
        rule_types: Iterable[str] = (),
        indices: Iterable[int] = (),
    ) -> list[dict]:
        """Remove rules in place; returns what was removed (see ``drop_rules``)."""
        self.payload, removed = drop_rules(
            self.payload, columns=columns, rule_types=rule_types, indices=indices,
        )
        if removed:
            self.notes.append(f"dropped {len(removed)} rule(s) in code")
        return removed

    def set_severity(
        self,
        severity: str,
        *,
        columns: Iterable[Optional[str]] = (),
        rule_types: Iterable[str] = (),
        indices: Iterable[int] = (),
    ) -> list[dict]:
        """Mark matching rules ``P1``/``P2``/``P3`` (see ``set_severity``); returns them."""
        self.payload, changed = set_severity(self.payload, severity, columns=columns,
                                             rule_types=rule_types, indices=indices)
        if changed:
            self.notes.append(f"severity {severity} on {len(changed)} rule(s)")
        return changed

    def relax(self, tolerance: float = 0.1) -> int:
        """Widen numeric ranges by ±``tolerance`` so they hold on the next partition."""
        self.payload, changed = relax_rules(self.payload, tolerance)
        if changed:
            self.notes.append(f"relaxed {changed} range(s) by ±{tolerance:.0%}")
        return changed

    # ── writing rules by hand ────────────────────────────────────────────

    @classmethod
    def new(cls, table: str = ADHOC_TABLE) -> "QualityDraft":
        """An empty draft — add rules with ``add_gx_expectation`` / ``expect_…`` / ``add_sql``.

        ``table`` only matters when the rules are saved to a contract; leave it out
        to check a DataFrame quickly::

            QualityDraft.new().expect_column_values_to_not_be_null("id").validate(df)
        """
        from redibis.quality.contract_writer import QualityContractWriter

        db, tbl = _split_table(table)
        payload = QualityContractWriter(database_name=db, table_name=tbl).build()
        return cls(table=table, payload=_canonicalize(_plain(payload)), engine="code")

    @classmethod
    def scan(cls, df: Any, table: str = ADHOC_TABLE, *, keep_snapshot_rules: bool = False,
             relax: float = 0.1, config: Any = None,
             log: Optional[Callable[[str], None]] = None) -> "QualityDraft":
        """Discover the rules of a pandas or Spark DataFrame, ready to edit::

            qa = QualityDraft.scan(df, "shop.customers")
            qa.remove("expect_column_values_to_match_regex", column="email")
            qa.expect_column_values_to_be_between("age", min_value=18, max_value=120)
            qa.validate(next_partition).to_frame(only_failed=True)

        Rules that only describe this exact sample (``SNAPSHOT_RULES``: row count,
        min/max/mean/median/stdev…) are dropped unless ``keep_snapshot_rules``, and
        the remaining numeric ranges are widened by ±``relax`` (``0`` keeps them).
        """
        draft = author_quality(df, table, config=config, log=log)
        if not keep_snapshot_rules:
            draft.drop(rule_types=SNAPSHOT_RULES)
        if relax:
            draft.relax(relax)
        if not keep_snapshot_rules or relax:
            draft.passed = draft.total = 0      # the discovery run no longer matches these rules
        return draft

    def remove(self, expectation: Optional[str] = None, column: Any = _ANY, *,
               index: Optional[int] = None) -> "QualityDraft":
        """Remove the rules that match **all** the given filters; returns the draft.

        ``qa.remove("expect_column_values_to_be_unique", column="email")``,
        ``qa.remove(column="notes")`` (every rule on a column), ``qa.remove(index=3)``.
        ``expectation`` also takes short names (``regex``, ``not_null``, ``sql``…);
        ``column=None`` means table-level rules. Raises ``ValueError`` when nothing matches.
        """
        if expectation is None and column is _ANY and index is None:
            raise ValueError("remove() needs an expectation, a column or an index")
        wanted = set()
        if expectation is not None:
            wanted = {str(expectation).lower()}
            wanted |= {_RULE_ALIASES[t] for t in wanted if t in _RULE_ALIASES}
        doomed = [r["index"] for r in self.rules
                  if (index is None or r["index"] == index)
                  and (column is _ANY or r["column"] == column)
                  and (not wanted or _rule_names(r) & wanted)]
        if not doomed:
            where = [f"{k}={v!r}" for k, v in (("expectation", expectation), ("column", column),
                                                ("index", index)) if v is not None and v is not _ANY]
            raise ValueError(f"no rule matches {', '.join(where)} — see draft.rules")
        self.drop(indices=doomed)
        return self

    def merge(self, *others: "QualityDraft", on_conflict: str = "replace") -> "QualityDraft":
        """Add other drafts' rules to this one, in place; returns this draft (calls chain)::

            auto = scan(df, "shop.customers")
            curated = QualityDraft.new("shop.customers").expect_column_values_to_be_between(
                "loyalty_points", min_value=0, max_value=100_000, severity="P1")
            auto.merge(curated)          # curated's range replaces the scanned one

        A *conflict* is a rule on the same column with the same check (the same
        expectation, or the same SQL query) but different arguments or severity.
        ``on_conflict``: ``"replace"`` (default — the draft merged in wins),
        ``"keep"`` (this draft's rule stays) or ``"both"`` (keep both rules).
        Identical rules are never duplicated. Drafts must be for the same table;
        a quick-check draft (``QualityDraft.new()`` without a table) merges into any.
        ``redibis.quality.merge(a, b, …)`` returns a new draft instead.
        """
        if on_conflict not in ("replace", "keep", "both"):
            raise ValueError("on_conflict must be 'replace', 'keep' or 'both'")
        for other in others:
            if not isinstance(other, QualityDraft):
                raise TypeError(f"merge() takes QualityDraft objects, not {type(other).__name__}")
            if other.table not in (self.table, ADHOC_TABLE):
                raise ValueError(f"cannot merge rules for {other.table!r} into {self.table!r}")
            mine = self._raw_rules()
            theirs = other._raw_rules()
            drop_mine: set[int] = set()
            skip: set[int] = set()
            for index, (key, entry) in enumerate(theirs):
                same = [i for i, (k, e) in enumerate(mine) if k == key]
                if any(mine[i][1] == entry for i in same):
                    skip.add(index)                     # identical: nothing to do
                elif same and on_conflict == "keep":
                    skip.add(index)
                elif same and on_conflict == "replace":
                    drop_mine.update(same)
            if drop_mine:
                self.payload, _ = drop_rules(self.payload, indices=drop_mine)
            extra, _ = drop_rules(other.payload, indices=skip)
            added = _merge_payload(self.payload, extra)
            self.notes.append(f"merged {len(added)} rule(s)"
                              + (f", replacing {len(drop_mine)}" if drop_mine else ""))
            if added or drop_mine:
                self.passed = self.total = 0            # no longer the rules that were validated
        return self

    def _raw_rules(self) -> list[tuple[tuple, Any]]:
        """``(conflict key, stored entry)`` per rule, in ``rules`` order."""
        out = []
        for (column, qlist, pos), r in zip(_iter_rule_slots(self.payload), self.rules):
            check = r["expectation_type"] or r["type"]
            query = " ".join(str(r["params"].get("query") or "").split()) if r["type"] == "sql" else ""
            out.append(((column, check, query), qlist[pos]))
        return out

    def add_gx_expectation(self, expectation_name: str, column: Optional[str] = None, *,
                           severity: Optional[str] = None, **kwargs: Any) -> "QualityDraft":
        """Add any Great Expectations rule — the same call the Quality page generates::

            qa = QualityDraft.new("shop.customers")
            qa.add_gx_expectation(
                expectation_name='expect_column_values_to_not_be_null', column='account_created_at')
            qa.add_gx_expectation(expectation_name='expect_column_values_to_be_between',
                                  column='loyalty_points', min_value=0, severity='P2')

        Returns the draft, so calls chain. ``severity`` (P1/P2/P3) is stored with the rule.
        """
        _check_expectation_name(expectation_name)
        if severity is not None and severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
        kwargs.pop("meta", None)
        kwargs.pop("batch_id", None)
        added = self.add_rules([{"rule": expectation_name, "column": column, "kwargs": kwargs}])
        if severity and added:
            self.set_severity(severity, indices=[r["index"] for r in added])
        return self

    def __getattr__(self, name: str):
        """``qa.expect_column_values_to_be_unique("id")`` — any expectation, called by its name."""
        if not name.startswith("expect_"):
            raise AttributeError(name)

        def add(column: Optional[str] = None, **kwargs: Any) -> "QualityDraft":
            return self.add_gx_expectation(name, column, **kwargs)

        add.__name__ = name
        add.__doc__ = f"Add ``{name}`` (keyword arguments as in Great Expectations; plus severity=)."
        return add

    def add_rules(self, rules: Iterable[dict]) -> list[dict]:
        """Add rules written by hand next to the discovered ones; returns them as listed.

        ``rules`` are ``RULES``-shape dicts — any Great Expectations expectation
        (``{"rule": "expect_…", "column": …, "kwargs": {…}}``) or ``sql_rule(...)``.
        Nothing is validated here; call ``validate(df)``.
        """
        extra = draft_from_rules(self.table, list(rules)).payload
        added = _merge_payload(self.payload, extra)
        if added:
            self.notes.append(f"added {len(added)} rule(s) in code")
        ids = {id(q) for q in added}
        return [listed for (_c, qlist, pos), listed in zip(_iter_rule_slots(self.payload), self.rules)
                if id(qlist[pos]) in ids]

    def add_sql(
        self,
        query: str,
        *,
        max_failures: int = 0,
        column: Optional[str] = None,
        description: Optional[str] = None,
        severity: Optional[str] = None,
        **thresholds: Any,
    ) -> dict:
        """Add a custom SQL rule; returns it as ``draft.rules`` lists it.

        ``query`` returns the rows that break the rule (``SELECT * FROM ${object}
        WHERE amount < 0``) or one number (``SELECT COUNT(*) …``); ``${object}``
        is this table. The rule passes when that count is at most
        ``max_failures`` — or pass ODCS thresholds, e.g. ``mustBeBetween=[0, 5]``.
        Stored at table level, or on ``column``. Check it with ``validate(df)``.
        """
        rule = sql_rule(query, max_failures=max_failures, column=column,
                        description=description, **thresholds)
        if severity is not None and severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
        entry = sql_rule_to_contract(rule)
        if severity:
            entry["severity"] = severity
        _attach_sql(self.payload, [], self.table)
        target = self.payload["schema"][0]
        if column:
            props = target.setdefault("properties", [])
            prop = next((p for p in props if p.get("name") == column), None)
            if prop is None:
                prop = {"name": column}
                props.append(prop)
            qlist = prop.setdefault("quality", [])
        else:
            qlist = target.setdefault("quality", [])
        if entry not in qlist:
            qlist.append(entry)
        self.notes.append("added SQL rule in code")
        for (col, ql, pos), listed in zip(_iter_rule_slots(self.payload), self.rules):
            if col == column and ql[pos] == entry:
                return listed
        return self.rules[-1]

    def validate(self, df: Any, *, spark_mode: str = "ge"):
        """Check these rules against another DataFrame (e.g. the next partition).

        Returns ``ContractQualityValidateResult`` — ``.status``, ``.rules_passed``,
        ``.rules_failed`` and per-rule ``.results``; ``.to_frame()`` as a table.
        Nothing is written. Drafts written by hand (``QualityDraft.new()``) name only
        the columns they check, so other columns are not reported as schema drift.
        On Spark, ``spark_mode="fused"`` runs the aggregate rules in one job
        (``"persist"``: Great Expectations on a cached frame).
        """
        from redibis.quality.contract_validate import validate_contract_quality

        result, _qa, _raw = validate_contract_quality(
            df, self.payload, table=self.table, include_schema_drift=self.engine != "code",
            spark_mode=spark_mode,
        )
        return result

    def to_rules(self) -> list[dict]:
        """The draft as generated-program ``RULES`` entries (``{rule, column, kwargs}``).

        ``RULES += discover_rules(df).to_rules()`` adds rules found on data to a
        program generated from the Quality page.
        """
        from redibis.quality.contract_validate import quality_rules_from_contract

        out = []
        for r in quality_rules_from_contract(self.payload).rules:
            item = {"rule": r["rule"], "column": r.get("column"),
                    "kwargs": dict(r.get("kwargs") or {})}
            if r.get("description"):
                item["description"] = r["description"]
            out.append(item)
        return out

    def to_code(self, var: str = "qa", *, style: str = "expect") -> str:
        """These rules as short, runnable Python — edit it, run it, ``validate(df)``::

            print(scan(df, "shop.customers").to_code())

            from redibis.quality import QualityDraft

            qa = QualityDraft.new("shop.customers")
            qa.expect_column_values_to_not_be_null("customer_id")
            qa.expect_column_values_to_be_in_set("country", value_set=["AE", "EG", "SA"], severity="P2")
            qa.add_sql("SELECT * FROM ${object} WHERE loyalty_points < 0", description="no negative points")

        ``style="expect"`` calls each expectation by name; ``style="gx"`` writes the
        ``add_gx_expectation(expectation_name=…)`` lines the Quality page generates.
        Severity is written when it is not the default ``P1``.
        """
        return "\n".join(["from redibis.quality import QualityDraft", "",
                          f"{var} = QualityDraft.new({self.table!r})",
                          *self.rule_lines(var, style=style)]) + "\n"

    def rule_lines(self, var: str = "qa", *, style: str = "expect") -> list[str]:
        """One Python statement per rule (``qa.expect_…(…)`` / ``qa.add_sql(…)``); see ``to_code``."""
        from redibis.quality.contract_validate import quality_rules_from_contract
        from redibis.quality.ge_codegen import _format_literal

        def lit(value: Any) -> str:
            if (isinstance(value, str) and "\\" in value and "'" not in value
                    and "\n" not in value and not value.endswith("\\")):
                return f"r'{value}'"                    # regexes read as written
            return _format_literal(value)

        if style not in ("expect", "gx"):
            raise ValueError("style must be 'expect' or 'gx'")
        lines: list[str] = []
        for r in quality_rules_from_contract(self.payload).rules:
            severity = (r.get("meta") or {}).get("severity")
            kwargs = dict(r.get("kwargs") or {})
            column = r.get("column")
            kwargs.pop("column", None)
            kwargs.pop("meta", None)
            for key, default in _GE_DEFAULTS.items():
                if key in kwargs and kwargs[key] == default:
                    del kwargs[key]
            kwargs = dict(sorted(kwargs.items(), key=lambda kv: (_ARG_ORDER.get(kv[0], 99), kv[0])))
            if r["rule"] == "sql":
                query = kwargs.pop("sql")
                if kwargs == {"mustBeLessThan": 1}:
                    kwargs = {}
                args = [lit(query)]
                if r.get("description"):
                    args.append(f"description={lit(r['description'])}")
                if column:
                    args.append(f"column={lit(column)}")
                call = f"{var}.add_sql"
            elif style == "gx":
                args = [f"expectation_name={lit(r['rule'])}"]
                if column:
                    args.append(f"column={lit(column)}")
                call = f"{var}.add_gx_expectation"
            else:
                args = [lit(column)] if column else []
                call = f"{var}.{r['rule']}"
            args += [f"{k}={lit(v)}" for k, v in kwargs.items()]
            if severity and severity != "P1":
                args.append(f"severity={lit(severity)}")
            one_line = f"{call}({', '.join(args)})"
            if len(one_line) <= 100:
                lines.append(one_line)
            else:
                lines.append(f"{call}(\n    " + ",\n    ".join(args) + ",\n)")
        return lines

    def to_program(self, engine: str = "spark") -> str:
        """The complete, Jupyter-ready program the Quality page generates, for these rules."""
        from redibis.services.quality_code import CuratedRules, render_quality_program

        return render_quality_program(
            table=self.table,
            curated=CuratedRules(contract=self.payload, rule_source="quality_draft"),
            engine=engine,
        )

    def summary(self) -> str:
        rules = self.rules
        cols = {r["column"] for r in rules if r["column"]}
        checked = (f"validation {self.passed}/{self.total} passed" if self.total
                   else "not validated yet — call validate(df)")
        head = (f"{self.table}: {len(rules)} rules on {len(cols)} columns "
                f"({self.engine}; {checked}"
                + (f"; {self.rows} rows" if self.rows is not None else "") + ")")
        body = [f"  [{r['index']:>3}] {r['column'] or TABLE_LEVEL:<24} {rule_label(r)}"
                for r in rules]
        return "\n".join([head, *body])

    def __repr__(self) -> str:  # notebooks print this
        return (f"QualityDraft(table={self.table!r}, rules={len(self.rules)}, "
                f"passed={self.passed}/{self.total}, engine={self.engine!r})")

    # ── file hand-off (notebook ↔ CLI) ──────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "format": DRAFT_FORMAT,
            "table": self.table,
            "engine": self.engine,
            "rows": self.rows,
            "validation": {"passed": self.passed, "total": self.total},
            "created_at": self.created_at,
            "notes": list(self.notes),
            "payload": self.payload,
        }

    def to_yaml(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True),
                     encoding="utf-8")
        return p

    @classmethod
    def from_dict(cls, data: dict, *, table: Optional[str] = None) -> "QualityDraft":
        """Accept a draft file or a bare ODCS quality partial (e.g. ``quality-run export``)."""
        if not isinstance(data, dict):
            raise ValueError("quality draft must be a mapping")
        if "payload" in data:
            payload = data["payload"] or {}
            validation = data.get("validation") or {}
            name = table or data.get("table") or ""
            draft = cls(
                table=name,
                payload=payload,
                passed=int(validation.get("passed") or 0),
                total=int(validation.get("total") or 0),
                engine=str(data.get("engine") or "pandas"),
                rows=data.get("rows"),
                created_at=str(data.get("created_at") or _utc_now_iso()),
                notes=list(data.get("notes") or []),
            )
        elif "schema" in data:
            db, tbl = data.get("database_name") or "", data.get("table_name") or ""
            name = table or (f"{db}.{tbl}" if db else tbl)
            draft = cls(table=name, payload=data, engine="file")
        else:
            raise ValueError("not a quality draft: expected 'payload' or an ODCS 'schema'")
        if not draft.table:
            raise ValueError("quality draft has no table name; pass table=")
        if not any(True for _ in _iter_rule_slots(draft.payload)):
            raise ValueError("quality draft contains no quality rules")
        return draft

    @classmethod
    def from_yaml(cls, path: str | Path, *, table: Optional[str] = None) -> "QualityDraft":
        """Load a draft file, an ODCS quality partial, or a generated program (``.py``).

        A ``.py`` program is **parsed, never executed**: only the literal
        ``RULES`` entries are read (the Quality page's AST-safe parser).
        """
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".py":
            return cls.from_program(text, table=table)
        return cls.from_dict(yaml.safe_load(text), table=table)

    @classmethod
    def from_program(cls, source: str, *, table: Optional[str] = None) -> "QualityDraft":
        """Rules from a generated / edited quality program — AST parse only."""
        import ast

        from redibis.services.quality_code import rules_from_python_source

        rules, errors = rules_from_python_source(source)
        if not rules:
            detail = "; ".join(f"line {e.get('line', 0)}: {e.get('message')}" for e in errors)
            raise ValueError("no quality rules found in the program"
                             + (f" ({detail})" if detail else ""))
        name = table or ""
        if not name:
            for node in ast.parse(source).body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and getattr(node.targets[0], "id", "") == "TABLE"
                        and isinstance(node.value, ast.Constant)):
                    name = str(node.value.value)
        if not name:
            raise ValueError("program has no TABLE = '...' line; pass the table name")
        draft = draft_from_rules(name, rules)
        draft.notes.append("imported from a quality program")
        return draft


# ─────────────────────────────────────────────────────────────────────────────
# Authoring
# ─────────────────────────────────────────────────────────────────────────────

def author_quality(
    df: Any,
    table: str,
    *,
    config: Any = None,
    count_rows: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> QualityDraft:
    """Discover + validate quality rules on a pandas or Spark DataFrame.

    Rule discovery runs Great Expectations' onboarding assistant on the whole
    frame (Spark: in the cluster, no collect). Only the lightweight triage
    signals use a 5 000-row sample. ``count_rows`` adds ``df.count()`` for Spark
    (an extra pass over the data); pandas rows are always known.
    """
    from redibis.config import RedibisConfig
    from redibis.profiling.great_expectations import GreatExpectationsProfiler
    from redibis.quality.rule_set import QualityRuleSet
    from redibis.quality.validator import GreatExpectationsValidator
    from redibis.scan.quality_phase import extract_quality_stats

    cfg = config or RedibisConfig.default()
    db, tbl = _split_table(table)
    spark = is_spark_dataframe(df)
    say = log or (lambda _m: None)

    say(f"discovering quality rules for {table} ({'spark' if spark else 'pandas'})…")
    profile = GreatExpectationsProfiler(cfg.profiling).profile(df, dataset_name=tbl)

    say("validating the discovered rules…")
    with tempfile.TemporaryDirectory(prefix="redibis_author_") as tmp:
        validator = GreatExpectationsValidator(
            suite_name=f"{tbl}_author_suite", in_memory=True, context_root_dir=Path(tmp),
        )
        validator.attach_dataframe(df, dataset_name=tbl)
        validator.apply_rules(QualityRuleSet(), profile)
        results = validator.run_tests(stage="author", generate_docs=False)
        stats = extract_quality_stats(results)
        payload = validator.export_quality_contract(
            database_name=db, table_name=tbl, column_dtypes=dtype_map_from_dataframe(df),
        )

    payload = _canonicalize(_plain(payload))
    payload, covered, counted = ensure_column_coverage(df, payload, spark=spark)
    if covered:
        say(f"added baseline rules for columns the profiler skipped: {', '.join(covered)}")

    rows: Optional[int] = None
    if not spark:
        rows = len(df)
    elif counted >= 0:
        rows = counted
    elif count_rows:
        rows = int(df.count())
    # Coverage rules hold by construction (computed on this same frame).
    added = sum(len(p.get("quality") or []) for schema_obj in payload.get("schema", []) or []
                for p in schema_obj.get("properties", []) or [] if p.get("name") in covered)
    draft = QualityDraft(
        table=table,
        payload=payload,
        passed=int(stats.get("passed") or 0) + added,
        total=int(stats.get("total") or 0) + added,
        engine="spark" if spark else "pandas",
        rows=rows,
    )
    if covered:
        draft.notes.append(f"baseline rules added for {', '.join(covered)}")
    say(f"{len(draft.rules)} rules; validation {draft.passed}/{draft.total} passed")
    return draft


def merge(*drafts: QualityDraft, on_conflict: str = "replace") -> QualityDraft:
    """A new draft with every draft's rules; later drafts win conflicts (see ``QualityDraft.merge``).

    ``merge(scan(df, "shop.orders"), curated)`` — the inputs are not changed.
    """
    if not drafts:
        raise ValueError("merge() needs at least one draft")
    first = next((d for d in drafts if d.table != ADHOC_TABLE), drafts[0])
    out = QualityDraft.new(first.table)
    out.engine = drafts[0].engine
    return out.merge(*drafts, on_conflict=on_conflict)


def draft_from_rules(table: str, rules: list[dict], *, df: Any = None) -> QualityDraft:
    """Rules in the generated-program shape (``{"rule", "column", "kwargs"}``) → draft.

    With ``df`` (pandas or Spark) the rules are validated on it first and the
    pass count is recorded; without it they are packaged as-is (CLI import).
    """
    from types import SimpleNamespace

    from redibis.quality.contract_writer import QualityContractWriter
    from redibis.quality.rule_set import QualityRuleSet
    from redibis.services.quality_code import _normalize_rule

    normalized = []
    sql_rules = []
    for r in rules or []:
        n = _normalize_rule(dict(r))
        if not n.get("rule"):
            continue
        if is_sql_rule(n):
            sql_rules.append(n)
            continue
        if n.get("column") and "column" not in n["kwargs"]:
            n["kwargs"]["column"] = n["column"]
        normalized.append(n)
    if not normalized and not sql_rules:
        raise ValueError("no quality rules to package")
    db, tbl = _split_table(table)
    passed = total = 0
    rows: Optional[int] = None
    spark = df is not None and is_spark_dataframe(df)
    if df is not None and normalized:
        from redibis.quality.gatekeeper import QualityGatekeeper
        from redibis.scan.quality_phase import extract_quality_stats
        from redibis.services.pipeline import apply_quality_rules

        qa = QualityGatekeeper(suite_name=f"{tbl}_notebook_suite", in_memory=True)
        qa.attach_dataframe(df, dataset_name=tbl)
        apply_quality_rules(qa, QualityRuleSet(name=tbl, rules=normalized),
                            profiler_expectations=[])
        stats = extract_quality_stats(qa.run_tests(stage="notebook", generate_docs=False))
        passed, total = int(stats.get("passed") or 0), int(stats.get("total") or 0)
        payload = qa.export_quality_contract(
            database_name=db, table_name=tbl, column_dtypes=dtype_map_from_dataframe(df),
        )
    else:
        writer = QualityContractWriter(database_name=db, table_name=tbl)
        writer.add_expectations([SimpleNamespace(expectation_type=n["rule"], kwargs=n["kwargs"])
                                 for n in normalized])
        payload = writer.build()
    if df is not None:
        rows = None if spark else len(df)
    if sql_rules:
        payload = _plain(payload)
        _attach_sql(payload, sql_rules, table)
        if df is not None:
            from redibis.quality.sql_rules import run_sql_rules

            checked = run_sql_rules(df, sql_rules, table=table)
            passed += sum(1 for row in checked if row.get("success"))
            total += len(checked)
    draft = QualityDraft(
        table=table,
        payload=_canonicalize(_plain(payload)),
        passed=passed,
        total=total,
        engine=("spark" if spark else "pandas") if df is not None else "program",
        rows=rows,
    )
    _apply_rule_severities(draft, [*normalized, *sql_rules])
    return draft


def _apply_rule_severities(draft: QualityDraft, rules: list[dict]) -> None:
    """Carry ``meta.severity`` of the input rules onto the drafted ones (P1 stays the default)."""
    listed = draft.rules
    for rule in rules:
        severity = (rule.get("meta") or {}).get("severity")
        if severity not in SEVERITIES or severity == "P1":
            continue
        column = rule.get("column")
        if is_sql_rule(rule):
            query = " ".join(str((rule.get("kwargs") or {}).get("sql") or "").split())
            hits = [r["index"] for r in listed if r["type"] == "sql" and r["column"] == column
                    and " ".join(str(r["params"].get("query") or "").split()) == query]
        else:
            name = str(rule.get("rule") or "").lower()
            hits = [r["index"] for r in listed if r["column"] == column and name in _rule_names(r)]
        if hits:
            draft.set_severity(severity, indices=hits)


class QualityAuthor:
    """Author quality rules in code and hand them to the contract lifecycle.

    Uses the same storage as the CLI: ``output_dir`` → ``<output_dir>/_dev_storage``
    (``redibis … --output-dir``), or pass ``backend=`` (e.g. S3/MinIO via
    ``QualityAuthor.from_s3``) to share a store between a cluster notebook and
    the CLI.
    """

    def __init__(
        self,
        table: str,
        *,
        output_dir: str | Path = "./reports",
        backend: Any = None,
        contracts_bucket: str = "active-contracts",
        quality_bucket: str = "quality-contracts",
        config: Any = None,
    ):
        from redibis.store.contract_store import ContractStore
        from redibis.store.run_merger import RunMerger
        from redibis.store.storage_backend import LocalBackend
        from redibis.store.subcontract_store import SubcontractStore

        self.table = table
        self.config = config
        self.backend = backend or LocalBackend(str(Path(output_dir) / "_dev_storage"))
        self.store = ContractStore(self.backend, contracts_bucket)
        self.runs_store = SubcontractStore(self.backend, quality_bucket=quality_bucket)
        self.merger = RunMerger(self.store, self.runs_store)

    @classmethod
    def from_s3(cls, table: str, *, endpoint: Optional[str] = None, **kwargs) -> "QualityAuthor":
        """Share the CLI's S3/MinIO store (credentials from the environment)."""
        from redibis.store.storage_backend import S3Config, get_backend

        s3 = S3Config.from_env()
        if endpoint:
            s3.endpoint_url = endpoint
        return cls(table, backend=get_backend(s3), **kwargs)

    # ── author ───────────────────────────────────────────────────────────

    def author(self, df: Any, *, count_rows: bool = False,
               log: Optional[Callable[[str], None]] = print) -> QualityDraft:
        return author_quality(df, self.table, config=self.config,
                              count_rows=count_rows, log=log)

    def save(self, draft: QualityDraft, *, run_id: Optional[str] = None,
             note: str = "", created_by: str = "code"):
        """Store the draft as a quality run (status ``draft``); returns the run."""
        if draft.table != self.table:
            raise ValueError(f"draft is for {draft.table!r}, not {self.table!r}")
        return save_draft(self.runs_store, draft, run_id=run_id, note=note,
                          created_by=created_by)

    # ── review ───────────────────────────────────────────────────────────

    def runs(self) -> list[dict]:
        return self.runs_store.list_run_summaries("quality", self.table)

    def load(self, run_id: Optional[str] = None) -> QualityDraft:
        """A stored run as a draft (latest non-discarded run by default)."""
        sub = latest_run(self.runs_store, self.table, run_id)
        return QualityDraft(table=self.table, payload=sub.payload,
                            passed=int(sub.summary_stats.get("quality_passed") or 0),
                            total=int(sub.summary_stats.get("quality_total") or 0),
                            engine=str(sub.summary_stats.get("engine") or "pandas"),
                            created_at=sub.created_at)

    def active_rules(self) -> list[dict]:
        active = self.store.get_active(self.table)
        return list_rules(active) if active else []

    def diff(self, run: str | QualityDraft | None = None) -> str:
        payload = run.payload if isinstance(run, QualityDraft) else self.load(run).payload
        return format_diff(diff_rules(self.store.get_active(self.table), payload))

    def update(self, run_id: str, draft: QualityDraft, *, note: str = "",
               edited_by: str = "code"):
        """Replace a stored run's rules with a curated draft (recorded as an edit)."""
        sub = self.merger.edit_run("quality", self.table, run_id, draft.payload,
                                   note=note or "updated from code", edited_by=edited_by)
        if sub is None:
            raise KeyError(f"no quality run {run_id!r} for {self.table}")
        return sub

    # ── merge ────────────────────────────────────────────────────────────

    def merge(self, run_id: Optional[str] = None, *, validate: bool = True):
        """Merge a run into the active contract (same path as ``redibis quality-run merge``)."""
        sub = latest_run(self.runs_store, self.table, run_id)
        return self.merger.merge_run("quality", self.table, sub.run_id,
                                     validate=validate, strip_pii_quality=True)


# ─────────────────────────────────────────────────────────────────────────────
# Shared with the CLI
# ─────────────────────────────────────────────────────────────────────────────

def save_draft(runs_store: Any, draft: QualityDraft, *, run_id: Optional[str] = None,
               note: str = "", created_by: str = "code"):
    rid = run_id or "author_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    stats = {
        "quality_passed": draft.passed,
        "quality_total": draft.total,
        "rules": len(draft.rules),
        "engine": draft.engine,
        "source": created_by,
    }
    if draft.rows is not None:
        stats["rows"] = draft.rows
    if note or draft.notes:
        stats["note"] = "; ".join([n for n in [note, *draft.notes] if n])
    return runs_store.create_from_payload(
        kind="quality", schema_table=draft.table, run_id=rid,
        payload=draft.payload, created_by=created_by, summary_stats=stats,
    )


def latest_run(runs_store: Any, table: str, run_id: Optional[str] = None):
    """The named quality run, or the newest non-discarded one."""
    if run_id:
        sub = runs_store.get("quality", table, run_id)
        if sub is None:
            raise KeyError(f"no quality run {run_id!r} for {table}")
        return sub
    for sub in runs_store.list_runs("quality", table):
        if sub.status != "discarded":
            return sub
    raise KeyError(f"no quality runs for {table}")


__all__ = [
    "SNAPSHOT_RULES",
    "QualityAuthor",
    "QualityDraft",
    "author_quality",
    "diff_rules",
    "draft_from_rules",
    "drop_rules",
    "format_diff",
    "latest_run",
    "list_rules",
    "relax_rules",
    "ensure_column_coverage",
    "rule_label",
    "save_draft",
]
