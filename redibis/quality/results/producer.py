"""Validate a partition **once**, when it is written, and store the results for every consumer.

    run = validate_partition(df, "shop.orders", partition="2026-09-26",
                             contract=load_rules_file(...),      # or a ContractStore
                             store=get_result_store(),           # Iceberg by default
                             fingerprint=snapshot_id,            # e.g. the Iceberg snapshot id
                             policies="policies/")               # consumers' custom SQL rules

The rule set is the table's contract rules **plus** every consumer's custom SQL
rules for that table, so each rule is computed once no matter how many
consumers need it. A run is skipped (and the stored one returned) when the same
``(table, partition, fingerprint, rule_set_digest)`` was already validated;
new data (new fingerprint) or new rules (new digest) trigger a new run.

Choosing the partition is the caller's job: ``df`` must already hold exactly
that partition (``spark.table(…).where("dt = '2026-09-26'")``). redibis never
filters it; it validates the frame and records the partition it is told
(column name, value, and a sortable ``partition_ts``).
"""

from __future__ import annotations

import copy
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from redibis.quality.results.records import (
    PartitionRun,
    partition_spec,
    to_json,
    utc_now,
)
from redibis.quality.results.store import QualityResultStore, get_result_store


def _is_spark(df: Any) -> bool:
    return hasattr(df, "sparkSession") and type(df).__module__.startswith("pyspark.")


def resolve_contract(contract: Any, table: str) -> dict:
    """A contract dict, a published rules file dict, or a ``ContractStore`` (active + decisions)."""
    if isinstance(contract, dict):
        return contract
    if hasattr(contract, "get_active"):
        from redibis.store.quality_decisions import effective_contract_quality

        active = contract.get_active(table)
        if active is None:
            raise LookupError(f"no active contract for {table}")
        return effective_contract_quality(active, contract.quality_decisions.get(table))
    raise TypeError("contract must be a dict or a ContractStore")


def with_consumer_rules(contract: dict, policies: Iterable[Any], table: str) -> tuple[dict, dict[str, str]]:
    """The contract plus the consumers' custom SQL rules, and ``rule_id → owner``."""
    from redibis.contracts.rules import stable_rule_id
    from redibis.quality.sql_rules import sql_rule_to_contract

    combined = copy.deepcopy(contract)
    schema = combined.setdefault("schema", [])
    if not schema:
        schema.append({"name": table.split(".")[-1], "properties": []})
    table_quality = schema[0].setdefault("quality", [])
    owners: dict[str, list[str]] = {}
    for policy in policies:
        if policy.table != table:
            continue
        for rule in policy.custom_sql_rules():
            entry = sql_rule_to_contract(rule)
            if entry not in table_quality:
                table_quality.append(entry)
            owners.setdefault(stable_rule_id(None, entry), []).append(f"consumer:{policy.consumer}")
    return combined, {rid: ",".join(sorted(set(o))) for rid, o in owners.items()}


def rule_set_rows(contract: dict, table: str, *, owners: dict[str, str],
                  contract_version: str) -> tuple[str, list[dict[str, Any]]]:
    """``(rule_set_digest, rows)`` describing every rule of ``contract``."""
    from redibis.quality.authoring import _iter_rule_slots, list_rules
    from redibis.quality.schema import rule_set_from_contract

    digest = rule_set_from_contract(contract, table=table).semantic_digest
    now = utc_now()
    rows = []
    for (_col, qlist, pos), r in zip(_iter_rule_slots(contract), list_rules(contract)):
        raw = qlist[pos] if isinstance(qlist[pos], dict) else {}
        rows.append({
            "table_name": table, "rule_set_digest": digest, "contract_version": contract_version,
            "rule_id": r["id"], "rule_type": r["type"], "odcs_rule": r["odcs_rule"],
            "expectation": r["expectation_type"] or ("sql" if r["type"] == "sql" else ""),
            "column_name": r["column"], "severity": r.get("severity") or "P1",
            "owner": owners.get(r["id"], "contract"),
            "description": str(raw.get("description") or ""),
            "definition": to_json(raw), "created_at": now,
        })
    return digest, rows


def validate_partition(
    df: Any,
    table: str,
    *,
    partition: Union[str, dict[str, Any]],
    contract: Any,
    store: Union[QualityResultStore, str, None] = None,
    partition_date: Optional[date] = None,
    fingerprint: str = "",
    policies: Union[str, Path, Iterable[Any], None] = None,
    run_id: Optional[str] = None,
    force: bool = False,
    count_rows: bool = False,
    spark_mode: str = "ge",
    partition_ts: Optional[datetime] = None,
) -> PartitionRun:
    """Validate one partition (pandas or Spark) and store the results; see module docstring.

    ``fingerprint`` identifies the data version (Iceberg snapshot id, a file
    checksum, a load id). Leave it empty to validate each partition once per
    rule set; pass ``force=True`` to re-validate anyway.

    ``partition`` names what ``df`` holds — ``"dt=2026-09-26"``,
    ``"dt=2026-09-26/hour=05"`` or ``{"dt": date(2026, 9, 26), "hour": 5}`` — and
    is recorded as ``partition_column`` / ``partition_value`` plus the typed
    ``partition_ts`` (override with ``partition_ts=``) and ``partition_date``.

    ``spark_mode="fused"`` computes the aggregate rules in one Spark job (and the
    row count with them); see ``validate_contract_quality``.
    """
    from redibis.quality.contract_validate import validate_contract_quality
    from redibis.quality.results.policy import load_policies

    store = store if isinstance(store, QualityResultStore) else get_result_store(store)
    loaded = load_policies(policies) if isinstance(policies, (str, Path)) else list(policies or [])
    base = resolve_contract(contract, table)
    version = str(base.get("contract_version") or base.get("version") or "")
    combined, owners = with_consumer_rules(base, loaded, table)
    digest, rule_rows = rule_set_rows(combined, table, owners=owners, contract_version=version)
    spec = partition_spec(partition, partition_ts=partition_ts)
    partition = spec.partition

    if not force:
        existing = store.find_run(table, partition, fingerprint, digest)
        if existing is not None:
            return existing
    store.save_rule_set(table, digest, rule_rows)

    run_id = run_id or f"{utc_now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    validated_at = utc_now()
    pdate = partition_date or spec.date
    where = {"table_name": table, "partition": partition, "partition_column": spec.column,
             "partition_value": spec.value, "partition_ts": spec.ts, "partition_date": pdate}
    spark = _is_spark(df)
    fused = spark and spark_mode == "fused"
    rows = None if spark and (fused or not count_rows) else (int(df.count()) if spark else len(df))
    result, _qa, _raw = validate_contract_quality(df, combined, table=table, run_id=run_id,
                                                  include_schema_drift=False, spark_mode=spark_mode)
    if rows is None and result.stats.get("row_count") is not None:
        rows = int(result.stats["row_count"])                # counted by the fused job, for free
    by_id = {r["rule_id"]: r for r in rule_rows}
    results = []
    for r in result.results:
        meta = by_id.get(r.rule_id, {})
        results.append({
            **where, "run_id": run_id, "validated_at": validated_at, "rule_set_digest": digest,
            "rule_id": r.rule_id, "rule_type": meta.get("rule_type", ""),
            "expectation": r.expectation_type, "column_name": r.column,
            "severity": r.severity or meta.get("severity") or "P1",
            "owner": meta.get("owner", "contract"), "passed": bool(r.success),
            "unexpected_count": int(r.unexpected_count or 0), "observed": to_json(r.observed_value),
            "sample": to_json([str(v) for v in (r.partial_unexpected or [])[:5]]),
            "message": r.message or "",
        })
    failed = [x for x in results if not x["passed"]]
    run = PartitionRun(
        table_name=table, partition=partition, partition_date=pdate, run_id=run_id,
        validated_at=validated_at, data_fingerprint=fingerprint, rule_set_digest=digest,
        contract_version=version, engine=result.engine or ("spark" if spark else "pandas"), row_count=rows,
        rules_total=len(results), rules_passed=len(results) - len(failed),
        p1_failed=sum(1 for x in failed if x["severity"] == "P1"),
        status="error" if result.error else ("failed" if failed else "passed"),
        error=result.error or "", partition_column=spec.column, partition_value=spec.value,
        partition_ts=spec.ts,
    )
    store.save_run(run, results)
    return run


def iceberg_snapshot_id(spark: Any, table_identifier: str) -> str:
    """The current snapshot id of an Iceberg table read through Spark — a good fingerprint."""
    row = spark.sql(f"SELECT snapshot_id FROM {table_identifier}.snapshots "
                    "ORDER BY committed_at DESC LIMIT 1").collect()
    return str(row[0][0]) if row else ""
