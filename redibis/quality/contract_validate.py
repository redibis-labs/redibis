"""Validate-only quality execution against an active contract (no writes)."""

from __future__ import annotations

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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def quality_rules_from_contract(contract: dict) -> QualityRuleSet:
    """Build a ``QualityRuleSet`` from active contract quality rules."""
    rules: list[dict[str, Any]] = []
    for qr in extract_rules(contract):
        ge = _rule_to_ge(qr)
        if not ge or not ge.get("expectation_type"):
            continue
        kwargs = dict(ge.get("kwargs") or {})
        col = qr.column or kwargs.get("column")
        if col and "column" not in kwargs:
            kwargs["column"] = col
        meta = {"redibis_rule_id": qr.rule_id, "severity": qr.severity, "source": qr.source}
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
    by_key: dict[tuple[str, str, str], QualityRule] = {}
    for qr in rules:
        ge = _rule_to_ge(qr)
        if not ge:
            continue
        etype = str(ge.get("expectation_type") or "")
        kwargs = dict(ge.get("kwargs") or {})
        col = qr.column or kwargs.get("column") or ""
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
        qr = by_key.get((etype, str(col or ""), sid))
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
                partial_unexpected=list(row.get("partial_unexpected_list") or []),
                observed_value=row.get("observed_value"),
                severity=severity,
                source=source,
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
) -> tuple[ContractQualityValidateResult, QualityGatekeeper, Any]:
    """
    Run approved contract rules against ``df`` without writing contracts.

    Returns ``(result, gatekeeper, raw_ge_results)``.
    """
    started = _utc_now_iso()
    rid = run_id or uuid.uuid4().hex[:12]
    rule_set = rule_set_override or quality_rules_from_contract(contract)
    canonical_rules = extract_rules(contract)

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
    qa = QualityGatekeeper(
        suite_name=suite_name or f"monitor_{table.replace('.', '_')}",
        in_memory=in_memory,
        context_root_dir=run_dir or "./ge_monitor",
    )
    qa.attach_dataframe(df, dataset_name=table.replace(".", "_"))

    from redibis.services.pipeline import apply_quality_rules

    apply_quality_rules(qa, rule_set, profiler_expectations=[])

    try:
        raw = qa.run_tests(stage="monitor", generate_docs=generate_docs and not in_memory)
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

    rows = list(report.get("results") or [])
    validated = _rule_rows_to_validate_results(rows, canonical_rules)
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
        ),
        qa,
        raw,
    )
