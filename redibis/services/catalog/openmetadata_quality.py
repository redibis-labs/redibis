"""OpenMetadata Data Quality publisher (test suites / cases / results).

Stable rule/result identity is backend-neutral and lives in
``redibis.quality.rule_identity`` — re-exported here for backward
compatibility with existing imports of this module.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from redibis.contracts.rules import stable_rule_id
from redibis.quality.rule_identity import quality_case_name, quality_stable_rule_id
from redibis.services.catalog.assertions import (
    AssertionKey,
    Facet,
    LedgerEntry,
    value_hash,
)
from redibis.services.catalog.mapping import schema_object

logger = logging.getLogger(__name__)


class _QualityClient(Protocol):
    def get_system_version(self) -> dict: ...

    def put(self, path: str, body: dict) -> dict: ...

    def put_raw(self, path: str, body: Any, *, content_type: str = "application/json") -> dict: ...


def map_test_status(outcome: str) -> str:
    """Map Redibis pass/fail/error → OM Success/Failed/Aborted."""
    key = (outcome or "").strip().lower()
    if key in ("pass", "passed", "success", "ok"):
        return "Success"
    if key in ("fail", "failed", "failure"):
        return "Failed"
    return "Aborted"


# Native OM test definitions for common GE expectations.
_GE_TO_OM_TEST_DEFINITION: dict[str, str] = {
    "expect_column_values_to_not_be_null": "columnValuesToBeNotNull",
    "expect_column_values_to_be_unique": "columnValuesToBeUnique",
    "expect_column_values_to_be_in_set": "columnValuesToBeInSet",
    "expect_table_columns_to_match_set": "tableColumnCountToEqual",
    "expect_table_row_count_to_be_between": "tableRowCountToBeBetween",
    "expect_table_row_count_to_equal": "tableRowCountToEqual",
    "expect_column_values_to_match_regex": "columnValuesToMatchRegex",
    "expect_column_values_to_be_between": "columnValuesToBeBetween",
    "expect_column_value_lengths_to_be_between": "columnValueLengthsToBeBetween",
}


def resolve_test_definition(expectation_type: str) -> tuple[str, bool]:
    """Return ``(test_definition_name, is_native)`` for an GE expectation type."""
    etype = (expectation_type or "").strip()
    native = _GE_TO_OM_TEST_DEFINITION.get(etype)
    if native:
        return native, True
    # Deterministic external name — visible in OM as non-native mapping.
    slug = etype.replace("expect_", "").replace("_", "")
    return f"redibis_{slug}", False


def map_results_for_publish(result: Any) -> list[dict]:
    """Adapt a legacy ``ContractQualityValidateResult`` dict *or* a canonical
    ``QualityRunV1`` dict (``redibis.quality.schema``) to ``publish_quality`` input.

    Legacy rows use ``success: bool`` + ``unexpected_count``; canonical rows
    use ``outcome: "pass"|"fail"|"error"|"skip"`` + ``failed_count``. Both are
    accepted so any result sink can hand either shape to this adapter.
    """
    if hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif isinstance(result, dict):
        payload = result
    else:
        return []
    rows: list[dict] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            item = {
                "rule_id": getattr(item, "rule_id", ""),
                "stable_id": getattr(item, "stable_id", ""),
                "success": getattr(item, "success", False),
                "expectation_type": getattr(item, "expectation_type", ""),
                "column": getattr(item, "column", None),
                "message": getattr(item, "message", ""),
                "unexpected_count": getattr(item, "unexpected_count", 0),
            }
        if "outcome" in item:
            success = item.get("outcome") == "pass"
            unexpected_count = item.get("failed_count", 0)
        else:
            success = bool(item.get("success"))
            unexpected_count = item.get("unexpected_count", 0)
        status = "pass" if success else "fail"
        rows.append({
            "rule_id": item.get("rule_id"),
            "stable_id": item.get("stable_id"),
            "name": quality_case_name(item.get("stable_id") or item.get("rule_id") or ""),
            "status": status,
            "result": item.get("message") or status,
            "expectation_type": item.get("expectation_type"),
            "column": item.get("column"),
            "testResultValue": [
                {"name": "unexpected_count", "value": str(unexpected_count)},
            ],
        })
    drift = payload.get("schema_drift")
    if isinstance(drift, dict) and (drift.get("added") or drift.get("dropped")):
        rows.append({
            "stable_id": "schema_drift",
            "name": "redibis_schema_drift",
            "status": "fail",
            "result": (
                f"added={drift.get('added') or []}; "
                f"dropped={drift.get('dropped') or []}"
            ),
        })
    return rows


def _rule_expectation(rule: Any) -> tuple[str, Optional[str], dict]:
    """Return (expectation_type, column, kwargs) from a QualityRule or ODCS dict."""
    if hasattr(rule, "engine_hint") and rule.engine_hint:
        hint = rule.engine_hint or {}
        return (
            str(hint.get("expectation_type") or rule.type or "custom"),
            getattr(rule, "column", None),
            dict(hint.get("kwargs") or {}),
        )
    if hasattr(rule, "type"):
        etype = str(rule.type or "custom")
        return etype, getattr(rule, "column", None), dict(getattr(rule, "params", None) or {})
    # raw ODCS quality dict
    impl = rule.get("implementation") if isinstance(rule, dict) else {}
    if isinstance(impl, dict) and impl.get("expectation_type"):
        return (
            str(impl["expectation_type"]),
            None,
            dict(impl.get("kwargs") or {}),
        )
    return str((rule or {}).get("rule") or "custom"), None, {}


def iter_contract_quality_rules(contract: dict, table: str) -> list[dict]:
    """Flatten column + table quality rules with stable ids for OM."""
    out: list[dict] = []
    try:
        obj = schema_object(contract, table)
    except ValueError:
        return out

    for q in obj.get("quality") or []:
        if not isinstance(q, dict):
            continue
        rid = stable_rule_id(None, q)
        etype, _col, kwargs = _rule_expectation(q)
        sid = quality_stable_rule_id(etype, None, kwargs)
        out.append({
            "rule_id": rid,
            "stable_id": sid,
            "name": quality_case_name(sid),
            "column": None,
            "expectation_type": etype,
            "kwargs": kwargs,
            "description": q.get("description") or etype,
        })

    for prop in obj.get("properties") or []:
        if not isinstance(prop, dict) or not prop.get("name"):
            continue
        col = prop["name"]
        for q in prop.get("quality") or []:
            if not isinstance(q, dict):
                continue
            rid = stable_rule_id(col, q)
            etype, _c, kwargs = _rule_expectation(q)
            if "column" not in kwargs and col:
                kwargs = {**kwargs, "column": col}
            sid = quality_stable_rule_id(etype, col, kwargs)
            out.append({
                "rule_id": rid,
                "stable_id": sid,
                "name": quality_case_name(sid),
                "column": col,
                "expectation_type": etype,
                "kwargs": kwargs,
                "description": q.get("description") or etype,
            })
    return out


def _entity_link(table_fqn: str, column: Optional[str] = None) -> str:
    if column:
        return f"<#E::table::{table_fqn}::columns::{column}>"
    return f"<#E::table::{table_fqn}>"


def _timestamp_ms(run_started_at: Optional[datetime] = None) -> int:
    dt = run_started_at or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def publish_quality(
    client: _QualityClient,
    contract: dict,
    table: str,
    *,
    table_fqn: str,
    results: Optional[list[dict]] = None,
    run_started_at: Optional[datetime] = None,
    min_version: str = "1.12.4",
    ledger_store: Any = None,
    artifact_ref: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Upsert TestSuite / TestCases / TestCaseResults. Idempotent by name.

    ``results`` items: ``{rule_id|stable_id|name, status, result?}``.
    Returns a telemetry dict (including degradation flags).
    """
    telemetry: dict[str, Any] = {
        "quality_enabled": True,
        "test_suite": None,
        "test_cases": 0,
        "results_written": 0,
        "degraded": False,
        "degrade_reason": "",
        "om_version": "",
    }

    try:
        ver = client.get_system_version() or {}
        telemetry["om_version"] = str(
            ver.get("version") or ver.get("revision") or ""
        )
    except Exception as exc:  # noqa: BLE001 — probe only
        telemetry["degraded"] = True
        telemetry["degrade_reason"] = f"version_probe_failed: {exc}"

    rules = iter_contract_quality_rules(contract, table)
    if not rules:
        telemetry["quality_enabled"] = False
        return telemetry

    suite_name = f"redibis_{table.replace('.', '_')}"
    suite_body = {
        "name": suite_name,
        "displayName": f"Redibis {table}",
        "description": f"Quality rules from redibis ODCS for {table}",
        "executable": True,
    }

    try:
        suite = client.put("/v1/dataQuality/testSuites", suite_body)
    except RuntimeError as exc:
        msg = str(exc)
        if any(code in msg for code in ("404", "405", "501")):
            telemetry["degraded"] = True
            telemetry["degrade_reason"] = f"testSuites_unavailable: {msg[:200]}"
            return telemetry
        raise

    suite_fqn = str(
        suite.get("fullyQualifiedName") or suite_name
    )
    telemetry["test_suite"] = suite_fqn

    # Index results by various keys
    by_key: dict[str, dict] = {}
    for r in results or []:
        if not isinstance(r, dict):
            continue
        for k in ("stable_id", "rule_id", "name"):
            if r.get(k):
                by_key[str(r[k])] = r

    ts = _timestamp_ms(run_started_at)
    ledger_entries: list[LedgerEntry] = []

    for rule in rules:
        case_name = rule["name"]
        entity_link = _entity_link(table_fqn, rule.get("column"))
        test_def, is_native = resolve_test_definition(rule.get("expectation_type") or "")
        case_body = {
            "name": case_name,
            "displayName": case_name,
            "description": rule.get("description") or "",
            "entityLink": entity_link,
            "testSuite": suite_fqn,
            "testDefinition": test_def,
            "parameterValues": [],
        }
        if not is_native:
            case_body["description"] = (
                f"{case_body['description']} [redibis GE: {rule.get('expectation_type')}]"
            ).strip()
        try:
            case = client.put("/v1/dataQuality/testCases", case_body)
        except RuntimeError as exc:
            msg = str(exc)
            if any(code in msg for code in ("404", "405", "501")):
                telemetry["degraded"] = True
                telemetry["degrade_reason"] = f"testCases_unavailable: {msg[:200]}"
                return telemetry
            # Idempotent name conflict → continue with constructed FQN
            msg_l = msg.lower()
            if (
                re.search(r"\b409\b", msg)
                or "already exists" in msg_l
                or "entity already exists" in msg_l
            ):
                case = {"fullyQualifiedName": f"{suite_fqn}.{case_name}"}
            else:
                raise

        case_fqn = str(
            case.get("fullyQualifiedName") or f"{suite_fqn}.{case_name}"
        )
        telemetry["test_cases"] = int(telemetry["test_cases"]) + 1

        key = AssertionKey(
            asset_fqn=table_fqn,
            column_path=str(rule.get("column") or ""),
            facet=Facet.QUALITY_TEST,
            value_key=rule["stable_id"],
        )
        ledger_entries.append(
            LedgerEntry(
                backend="openmetadata",
                key=key,
                value_hash=value_hash(case_fqn),
                scan_id="",
                published_at=datetime.now(timezone.utc),
                state="confirmed",
            )
        )

        match = (
            by_key.get(rule["stable_id"])
            or by_key.get(rule["rule_id"])
            or by_key.get(case_name)
        )
        if not match:
            continue

        status = map_test_status(str(match.get("status") or match.get("outcome") or ""))
        result_msg = str(match.get("result") or match.get("message") or status)
        if artifact_ref:
            result_msg = f"{result_msg} | artifact={artifact_ref}"
        if run_id:
            result_msg = f"{result_msg} | run_id={run_id}"
        result_body = {
            "timestamp": ts,
            "testCaseStatus": status,
            "result": result_msg,
            "testResultValue": match.get("testResultValue") or [],
        }
        try:
            client.put(
                f"/v1/dataQuality/testCases/{case_fqn}/testCaseResult",
                result_body,
            )
            telemetry["results_written"] = int(telemetry["results_written"]) + 1
        except RuntimeError as exc:
            msg = str(exc)
            if any(code in msg for code in ("404", "405", "501")):
                telemetry["degraded"] = True
                telemetry["degrade_reason"] = f"testCaseResult_unavailable: {msg[:200]}"
                return telemetry
            raise

    if ledger_store is not None and ledger_entries:
        try:
            ledger_store.upsert_entries(table, "openmetadata", ledger_entries)
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist quality ledger entries")

    return telemetry


__all__ = [
    "iter_contract_quality_rules",
    "map_results_for_publish",
    "map_test_status",
    "publish_quality",
    "quality_case_name",
    "quality_stable_rule_id",
    "resolve_test_definition",
]
