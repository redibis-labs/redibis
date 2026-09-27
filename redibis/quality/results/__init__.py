"""Quality results: validate each partition once, store the results, let consumers decide.

Producer (the job that writes a partition)::

    from redibis.quality.results import get_result_store, validate_partition
    run = validate_partition(df, "shop.orders", partition="2026-09-26", contract=rules,
                             store=get_result_store(), fingerprint=snapshot_id, policies="policies/")

Consumer (a pipeline that reads the table)::

    from redibis.quality.results import ConsumerPolicy
    decision = ConsumerPolicy.from_yaml("policies/churn_model.yaml").evaluate()
    if not decision.accepted:
        raise SystemExit(decision)
    df = spark.table("lake.shop.orders").where(decision.partition_filter("dt"))

Stores (strategy pattern, :func:`get_result_store`): Iceberg (default), Parquet on
S3/MinIO or a local path, Postgres. See ``docs/QUALITY_RESULTS.md``.
"""

from redibis.quality.results.policy import (
    ConsumerDecision,
    ConsumerPolicy,
    PartitionVerdict,
    RuleSelection,
    Window,
    load_policies,
    rules_catalog,
)
from redibis.quality.results.producer import iceberg_snapshot_id, validate_partition
from redibis.quality.results.records import PartitionRun
from redibis.quality.results.store import QualityResultStore, get_result_store, register_store

__all__ = [
    "ConsumerDecision", "ConsumerPolicy", "PartitionRun", "PartitionVerdict", "QualityResultStore",
    "RuleSelection", "Window", "get_result_store", "iceberg_snapshot_id", "load_policies",
    "register_store", "rules_catalog", "validate_partition",
]
