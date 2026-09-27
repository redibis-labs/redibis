"""Validate-only quality execution against an active contract (no writes)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import pandas as pd

from redibis.contracts.rules import QualityRule, _rule_to_ge, extract_rules
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.quality.rule_set import QualityRuleSet
from redibis.quality.rule_identity import quality_stable_rule_id
from redibis.quality.validate_models import (
    ContractQualityValidateResult,
    RuleValidateResult,
    SchemaDriftResult,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def quality_rules_from_contract(contract: dict) -> QualityRuleSet:
    """Build a ``QualityRuleSet`` from active contract quality rules."""
    from redibis.quality.sql_rules import sql_rule_from_contract

    rules: list[dict[str, Any]] = []
    for qr in extract_rules(contract):
        meta = {"redibis_rule_id": qr.rule_id, "severity": qr.severity, "source": qr.source}
        if qr.type == "sql":
            entry = {**qr.params, "description": qr.description}
            rules.append({**sql_rule_from_contract(entry, qr.column), "meta": meta})
            continue
        ge = _rule_to_ge(qr)
        if not ge or not ge.get("expectation_type"):
            continue
        kwargs = dict(ge.get("kwargs") or {})
        col = qr.column or kwargs.get("column")
        if col and "column" not in kwargs:
            kwargs["column"] = col
        rules.append({
            "rule": ge["expectation_type"],
            "column": col,
            "kwargs": kwargs,
            "meta": meta,
        })
    return QualityRuleSet(rules=rules)


def compute_schema_drift(
    contract: dict,
    observed_columns: Sequence[str],
    *,
    table: str = "",
) -> SchemaDriftResult:
    """Compare observed sample columns to the active contract column set."""
    contract_cols: list[str] = []
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            name = prop.get("name")
            if name:
                contract_cols.append(str(name))
    contract_set = set(contract_cols)
    observed_set = {str(c) for c in observed_columns}
    return SchemaDriftResult(
        table=table,
        contract_columns=sorted(contract_set),
        observed_columns=sorted(observed_set),
        added=sorted(observed_set - contract_set),
        dropped=sorted(contract_set - observed_set),
    )


def _rule_rows_to_validate_results(
    report_rows: list[dict[str, Any]],
    rules: list[QualityRule],
) -> list[RuleValidateResult]:
    """Map gatekeeper normalized rows back to contract rule ids."""
    by_id: dict[str, QualityRule] = {qr.rule_id: qr for qr in rules}
    by_key: dict[tuple[str, str, str], QualityRule] = {}
    for qr in rules:
        ge = _rule_to_ge(qr)
        if not ge:
            continue
        etype = str(ge.get("expectation_type") or "")
        kwargs = dict(ge.get("kwargs") or {})
        col = qr.column or kwargs.pop("column", None) or ""
        kwargs.pop("column", None)   # report rows carry kwargs without the column
        sid = quality_stable_rule_id(etype, col or None, kwargs)
        by_key[(etype, str(col or ""), sid)] = qr

    out: list[RuleValidateResult] = []
    for row in report_rows:
        etype = str(row.get("rule") or "")
        kwargs = dict(row.get("kwargs") or {})
        col = kwargs.get("column") or row.get("column")
        if col == "Table-Level":
            col = None
        sid = quality_stable_rule_id(etype, col, kwargs)
        # Rules built from the contract carry their id in meta; match on it first.
        meta_id = str((row.get("meta") or {}).get("redibis_rule_id") or "")
        qr = by_id.get(meta_id) or by_key.get((etype, str(col or ""), sid))
        rule_id = qr.rule_id if qr else sid
        severity = (qr.severity if qr else "P1")
        source = (qr.source if qr else "contract")
        out.append(
            RuleValidateResult(
                rule_id=rule_id,
                expectation_type=etype,
                column=col,
                success=bool(row.get("success")),
                element_count=row.get("element_count"),
                unexpected_count=int(row.get("unexpected_count") or 0),
                unexpected_percent=float(row.get("unexpected_pct") or row.get("unexpected_percent") or 0.0),
                partial_unexpected=list(row.get("partial_unexpected")
                                        or row.get("partial_unexpected_list") or []),
                observed_value=row.get("observed_value"),
                severity=severity,
                source=source,
                message=str(row.get("reason") or ""),
                stable_id=sid,
            )
        )
    return out


def _rule_columns(rule: dict[str, Any]) -> list[str]:
    """Every column a GE-shaped rule reads (``column``, ``column_A/B``, ``column_list``)."""
    if str(rule.get("rule") or "").startswith("expect_table_"):
        return []   # table-shape rules *describe* the columns; they must run and report
    kwargs = rule.get("kwargs") or {}
    cols = [rule.get("column"), kwargs.get("column"), kwargs.get("column_A"), kwargs.get("column_B")]
    cols += list(kwargs.get("column_list") or [])
    return [str(c) for c in cols if c]


def _missing_column_results(rules: list[dict[str, Any]], present: set[str]) -> tuple[
        list[dict[str, Any]], list[RuleValidateResult]]:
    """Split off rules that read a column the data does not have.

    Running them would fail — and on Spark one failing metric can take the
    whole validation down with it — so they are reported as failed, with the
    reason, and the rest run normally.
    """
    runnable: list[dict[str, Any]] = []
    missing: list[RuleValidateResult] = []
    for rule in rules:
        absent = [c for c in _rule_columns(rule) if c not in present]
        if not absent:
            runnable.append(rule)
            continue
        meta = dict(rule.get("meta") or {})
        kwargs = {k: v for k, v in (rule.get("kwargs") or {}).items() if k != "column"}
        sid = quality_stable_rule_id(str(rule.get("rule")), rule.get("column"), kwargs)
        missing.append(RuleValidateResult(
            rule_id=str(meta.get("redibis_rule_id") or sid),
            expectation_type=str(rule.get("rule")),
            column=rule.get("column"),
            success=False,
            observed_value=None,
            severity=str(meta.get("severity") or "P1"),
            source=str(meta.get("source") or "contract"),
            message=f"column missing from the data: {', '.join(sorted(set(absent)))}",
            stable_id=sid,
        ))
    return runnable, missing


def _row_key(rule_or_row: dict[str, Any]) -> tuple[str, str]:
    """Match a report row to the rule it came from: rule id, else type + column + kwargs."""
    meta = rule_or_row.get("meta") or {}
    if meta.get("redibis_rule_id"):
        return ("id", str(meta["redibis_rule_id"]))
    kwargs = {k: v for k, v in (rule_or_row.get("kwargs") or {}).items()
              if k not in ("column", "meta", "batch_id")}
    column = rule_or_row.get("column") or (rule_or_row.get("kwargs") or {}).get("column")
    if column == "Table-Level":
        column = None
    return ("rule", repr((rule_or_row.get("rule"), column, sorted(kwargs.items(), key=str))))


# Expectations Great Expectations runs as Python functions on Spark workers — the
# usual cause of a bundle failure — are re-run on their own first.
_UDF_SUSPECTS = (
    "expect_column_values_to_match_json_schema",
    "expect_column_values_to_be_json_parseable",
    "expect_column_values_to_match_strftime_format",
    "expect_column_values_to_be_dateutil_parseable",
)


def _isolate_errors(df: Any, rows: list[dict[str, Any]], rules: list[dict[str, Any]],
                    table: str) -> list[dict[str, Any]]:
    """Re-run rules whose result is an engine exception until each is isolated.

    Great Expectations computes metrics in bundles; on Spark one rule that
    raises (e.g. ``match_json_schema`` on a value that is not JSON) fails every
    rule in its bundle. The errored rules are run again as a group, then in
    halves, until the rules that really cannot run stand alone — their rows
    keep the engine's message; every other rule gets its real result.
    """
    from redibis.services.pipeline import apply_quality_rules

    def run(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        qa = QualityGatekeeper(suite_name=f"rerun_{table.replace('.', '_')}", in_memory=True)
        qa.attach_dataframe(df, dataset_name=table.replace(".", "_"))
        apply_quality_rules(qa, QualityRuleSet(rules=group), profiler_expectations=[])
        return list(qa._extract_report_data(qa.run_tests(stage="rerun", generate_docs=False))
                    .get("results") or [])

    by_key = {_row_key(r): r for r in rules}
    final: dict[tuple[str, str], dict[str, Any]] = {}
    pending = [by_key[k] for k in (_row_key(r) for r in rows if r.get("exception")) if k in by_key]
    if not pending:
        return rows
    suspects = [r for r in pending if r.get("rule") in _UDF_SUSPECTS]
    rest = [r for r in pending if r.get("rule") not in _UDF_SUSPECTS]
    queue = ([rest] if rest else []) + [[r] for r in suspects]
    while queue:
        group = queue.pop()
        try:
            result_rows = run(group)
        except Exception as exc:  # noqa: BLE001
            result_rows = [{"rule": r.get("rule"), "column": r.get("column"), "kwargs": r.get("kwargs"),
                            "meta": r.get("meta"), "success": False,
                            "exception": f"{type(exc).__name__}: {exc}"[:500]} for r in group]
        still = []
        for row in result_rows:
            key = _row_key(row)
            if row.get("exception") and len(group) > 1 and key in by_key:
                still.append(by_key[key])
            else:
                final[key] = row
        if still:
            half = max(1, len(still) // 2)
            queue.extend(part for part in (still[:half], still[half:]) if part)
    out = []
    for row in rows:
        new = final.get(_row_key(row), row) if row.get("exception") else row
        if new.get("exception"):
            new = {**new, "success": False, "reason": f"could not run: {new['exception']}"}
        out.append(new)
    return out


def _sql_rows_to_validate_results(rows: list[dict[str, Any]]) -> list[RuleValidateResult]:
    """SQL rule report rows (``quality.sql_rules``) → validate results."""
    out: list[RuleValidateResult] = []
    for row in rows:
        col = row.get("column")
        col = None if col == "Table-Level" else col
        meta = dict(row.get("meta") or {})
        sid = quality_stable_rule_id("sql", col, dict(row.get("kwargs") or {}))
        out.append(
            RuleValidateResult(
                rule_id=str(meta.get("redibis_rule_id") or sid),
                expectation_type="sql",
                column=col,
                success=bool(row.get("success")),
                element_count=row.get("element_count"),
                unexpected_count=int(row.get("unexpected_count") or 0),
                unexpected_percent=float(row.get("unexpected_pct") or 0.0),
                partial_unexpected=list(row.get("partial_unexpected") or []),
                observed_value=row.get("observed_value"),
                severity=str(meta.get("severity") or "P1"),
                source=str(meta.get("source") or "contract"),
                message=str(row.get("reason") or ""),
                stable_id=sid,
            )
        )
    return out


def validate_contract_quality(
    df: pd.DataFrame,
    contract: dict,
    *,
    table: str,
    run_id: Optional[str] = None,
    include_schema_drift: bool = True,
    generate_docs: bool = False,
    run_dir: Optional[str] = None,
    suite_name: Optional[str] = None,
    rule_set_override: Optional[QualityRuleSet] = None,
    spark_mode: str = "ge",
) -> tuple[ContractQualityValidateResult, QualityGatekeeper, Any]:
    """
    Run approved contract rules against ``df`` without writing contracts.

    ``spark_mode`` (Spark DataFrames only):

    - ``"ge"`` (default): Great Expectations computes every rule, several Spark jobs each.
    - ``"persist"``: the same, on ``df.persist()``, so the source is read once.
    - ``"fused"``: rules that are aggregates (nulls, sets, ranges, regex, lengths,
      uniqueness, statistics, row counts, schema) run as **one** ``df.agg`` job
      (``redibis.quality.spark_fused``); the others run with Great Expectations on
      the persisted frame. Results have the same shape either way.

    Returns ``(result, gatekeeper, raw_ge_results)``.
    """
    if spark_mode not in ("ge", "persist", "fused"):
        raise ValueError("spark_mode must be 'ge', 'persist' or 'fused'")
    spark = hasattr(df, "sparkSession") and hasattr(df, "rdd")
    if not spark or spark_mode == "ge":
        result = _validate(df, contract, table=table, run_id=run_id,
                           include_schema_drift=include_schema_drift, generate_docs=generate_docs,
                           run_dir=run_dir, suite_name=suite_name,
                           rule_set_override=rule_set_override, fused=False)
        result[0].engine = "spark" if spark else "pandas"
        return result
    cached = bool(getattr(df, "is_cached", False))
    if not cached:
        df.persist()
    try:
        result = _validate(df, contract, table=table, run_id=run_id,
                           include_schema_drift=include_schema_drift, generate_docs=generate_docs,
                           run_dir=run_dir, suite_name=suite_name,
                           rule_set_override=rule_set_override, fused=spark_mode == "fused")
    finally:
        if not cached:
            df.unpersist()
    result[0].engine = f"spark-{spark_mode}"
    return result


def _validate(
    df: Any,
    contract: dict,
    *,
    table: str,
    run_id: Optional[str],
    include_schema_drift: bool,
    generate_docs: bool,
    run_dir: Optional[str],
    suite_name: Optional[str],
    rule_set_override: Optional[QualityRuleSet],
    fused: bool,
) -> tuple[ContractQualityValidateResult, QualityGatekeeper, Any]:
    started = _utc_now_iso()
    rid = run_id or uuid.uuid4().hex[:12]
    from redibis.quality.sql_rules import is_sql_rule

    rule_set = rule_set_override or quality_rules_from_contract(contract)
    canonical_rules = extract_rules(contract)
    sql_rules = [r for r in rule_set.rules if is_sql_rule(r)]
    ge_rules, missing_results = _missing_column_results(
        [r for r in rule_set.rules if not is_sql_rule(r)], {str(c) for c in df.columns})
    fused_rows: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    if fused and ge_rules:
        from redibis.quality.spark_fused import run_fused

        try:
            run = run_fused(df, ge_rules)
        except Exception as exc:  # noqa: BLE001 — a runtime error in the fused job: GE runs them all
            logger.warning("fused validation failed, running every rule with Great Expectations: %s", exc)
            stats = {"fused": 0, "ge": len(ge_rules), "fused_error": f"{type(exc).__name__}: {exc}"}
        else:
            fused_rows, ge_rules = run.rows, run.fallback
            stats = {"fused": run.fused, "ge": len(ge_rules), "spark_jobs_fused": run.spark_jobs,
                     "row_count": run.row_count}
    ge_rule_set = QualityRuleSet(name=rule_set.name, rules=ge_rules)

    if not rule_set.rules and include_schema_drift:
        drift = compute_schema_drift(contract, list(df.columns), table=table)
        status = "failed" if drift.added or drift.dropped else "success"
        return (
            ContractQualityValidateResult(
                table=table,
                run_id=rid,
                status=status,
                rules_total=0,
                rules_passed=0,
                rules_failed=0,
                schema_drift=drift,
                started_at=started,
                completed_at=_utc_now_iso(),
            ),
            QualityGatekeeper(in_memory=True),
            None,
        )

    in_memory = not (generate_docs and run_dir)
    if fused and not ge_rule_set.rules:
        qa = QualityGatekeeper(in_memory=True)       # everything ran fused: no GE context needed
    else:
        qa = _gatekeeper(df, table, suite_name, in_memory, run_dir)
        from redibis.services.pipeline import apply_quality_rules

        apply_quality_rules(qa, ge_rule_set, profiler_expectations=[])
    return _finish(df, contract, table, rid, started, rule_set, canonical_rules, sql_rules,
                   ge_rule_set, missing_results, fused_rows, stats, qa, include_schema_drift,
                   generate_docs and not in_memory)


def _gatekeeper(df: Any, table: str, suite_name: Optional[str], in_memory: bool,
                run_dir: Optional[str]) -> QualityGatekeeper:
    qa = QualityGatekeeper(
        suite_name=suite_name or f"monitor_{table.replace('.', '_')}",
        in_memory=in_memory,
        context_root_dir=run_dir or "./ge_monitor",
    )
    qa.attach_dataframe(df, dataset_name=table.replace(".", "_"))
    return qa


def _finish(df: Any, contract: dict, table: str, rid: str, started: str, rule_set: QualityRuleSet,
            canonical_rules: list, sql_rules: list, ge_rule_set: QualityRuleSet,
            missing_results: list, fused_rows: list, stats: dict, qa: QualityGatekeeper,
            include_schema_drift: bool, generate_docs: bool,
            ) -> tuple[ContractQualityValidateResult, QualityGatekeeper, Any]:
    from redibis.quality.sql_rules import run_sql_rules

    raw = None
    report: dict[str, Any] = {"results": []}
    try:
        if ge_rule_set.rules:
            raw = qa.run_tests(stage="monitor", generate_docs=generate_docs)
            report = qa._extract_report_data(raw)
    except Exception as exc:  # noqa: BLE001
        return (
            ContractQualityValidateResult(
                table=table,
                run_id=rid,
                status="error",
                rules_total=len(rule_set.rules),
                rules_passed=0,
                rules_failed=0,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started,
                completed_at=_utc_now_iso(),
            ),
            qa,
            None,
        )

    rows = _isolate_errors(df, list(report.get("results") or []), ge_rule_set.rules, table)
    validated = _rule_rows_to_validate_results(fused_rows + rows, canonical_rules)
    validated += _sql_rows_to_validate_results(run_sql_rules(df, sql_rules, table=table))
    validated += missing_results
    passed = sum(1 for r in validated if r.success)
    failed = len(validated) - passed
    drift = None
    if include_schema_drift:
        drift = compute_schema_drift(contract, list(df.columns), table=table)
    schema_failed = bool(drift and (drift.added or drift.dropped))
    status = "success" if failed == 0 and not schema_failed else "failed"

    return (
        ContractQualityValidateResult(
            table=table,
            run_id=rid,
            status=status,
            rules_total=len(validated),
            rules_passed=passed,
            rules_failed=failed,
            schema_drift=drift,
            results=validated,
            started_at=started,
            completed_at=_utc_now_iso(),
            stats=stats,
        ),
        qa,
        raw,
    )
