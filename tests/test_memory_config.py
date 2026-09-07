"""Tests for memory/config blocks (T1) and metadata retriever registry (T2–T3)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from redibis.config import ConfigError, JdbcConfig, MemoryConfig, RedibisConfig, SourceConfig
from redibis.metadata import (
    GenericJdbcRetriever,
    HiveMetastoreRetriever,
    aggregate_sql,
    get_metadata_retriever,
)
from redibis.metadata.credentials import resolve_credential_ref
from redibis.services.scan_service import ScanConfig, to_scan_config


def test_memory_config_defaults_off():
    cfg = RedibisConfig.default()
    assert cfg.source.engine == "none"
    assert cfg.profiling.metadata.enabled is False
    assert cfg.profiling.pushdown.enabled is False
    assert cfg.memory.enabled is False
    assert cfg.memory.store == "pgvector"
    assert cfg.memory.embedding_provider == "sentence_transformers"


def test_memory_blocks_yaml_round_trip(tmp_path):
    path = tmp_path / "memory.yaml"
    cfg = RedibisConfig.default()
    cfg.source.engine = "hive"
    cfg.profiling.metadata.enabled = True
    cfg.profiling.pushdown.enabled = True
    cfg.memory.enabled = True
    cfg.memory.domain = "telecom"
    cfg.to_yaml(path)

    loaded = RedibisConfig.from_yaml(path)
    assert loaded.source.engine == "hive"
    assert loaded.profiling.metadata.enabled is True
    assert loaded.profiling.pushdown.enabled is True
    assert loaded.memory.enabled is True
    assert loaded.memory.domain == "telecom"


def test_to_scan_config_unchanged_with_memory_flags_off():
    cfg = RedibisConfig.default()
    cfg.table = "telecom.customers"
    cfg.pii.equation_mode = "balanced"
    cfg.contract.automerge = "pii"

    baseline = ScanConfig(
        table="telecom.customers",
        equation_mode="balanced",
        automerge="pii",
    )
    projected = to_scan_config(cfg)
    assert projected.table == baseline.table
    assert projected.equation_mode == baseline.equation_mode
    assert projected.automerge == baseline.automerge
    assert projected.run_pii == baseline.run_pii
    assert projected.run_quality == baseline.run_quality
    assert projected.run_profile == baseline.run_profile
    assert projected.profiler_engine == baseline.profiler_engine


@pytest.mark.parametrize("field,value,match", [
    ("source", {"engine": "mysql"}, "source engine"),
    ("memory", {"store": "chroma"}, "memory store"),
])
def test_validate_rejects_unknown_memory_source_values(field, value, match):
    with pytest.raises(ConfigError, match=match):
        RedibisConfig.from_dict({field: value})


def test_get_metadata_retriever_hive():
    retriever = get_metadata_retriever(
        SourceConfig(engine="hive"),
        spark=MagicMock(),
    )
    assert isinstance(retriever, HiveMetastoreRetriever)


def test_get_metadata_retriever_jdbc_engines():
    for engine in ("postgres", "oracle", "jdbc"):
        retriever = get_metadata_retriever(
            SourceConfig(engine=engine),
            connection=MagicMock(),
        )
        assert isinstance(retriever, GenericJdbcRetriever)


def test_get_metadata_retriever_unknown_engine():
    with pytest.raises(ConfigError, match="unknown source engine"):
        get_metadata_retriever(SourceConfig(engine="none"))


def test_metadata_abc_importable_without_db_drivers():
    import importlib

    mod = importlib.import_module("redibis.metadata.base")
    assert hasattr(mod, "MetadataRetriever")


def test_aggregate_sql_hive_approx_distinct():
    sql = aggregate_sql(
        "hive",
        "approx_distinct",
        table="db.tbl",
        column="col_a",
        approx_distinct=True,
    )
    assert "APPROX_COUNT_DISTINCT" in sql
    assert "`db`.`tbl`" in sql


def test_aggregate_sql_exact_distinct_when_configured():
    sql = aggregate_sql(
        "postgres",
        "approx_distinct",
        table="db.tbl",
        column="col_a",
        approx_distinct=False,
    )
    assert "COUNT(DISTINCT" in sql
    assert "APPROX" not in sql


def test_aggregate_sql_sqlite_avoids_filter_clause():
    sql = aggregate_sql("sqlite", "null_count", table="order_header", column="amount")
    assert "SUM(CASE WHEN" in sql
    assert "FILTER" not in sql


def test_hive_retriever_populates_source_metadata():
    spark = MagicMock()
    col = MagicMock()
    col.name = "national_id"
    col.dataType = "string"
    col.nullable = False
    spark.catalog.listColumns.return_value = [col]

    describe_row = MagicMock()
    describe_row.asDict.return_value = {
        "col_name": "numRows",
        "data_type": "1000",
        "comment": "",
    }
    spark.sql.return_value.collect.return_value = [describe_row]

    retriever = HiveMetastoreRetriever(SourceConfig(engine="hive"), spark=spark)
    meta = retriever.get_source_metadata("telecom.customers")

    assert meta.engine == "hive"
    assert len(meta.columns) == 1
    assert meta.columns[0].name == "national_id"
    assert meta.table_props.get("numRows") == "1000"
    assert "national_id" in meta.column_stats
    assert meta.column_stats["national_id"].stats_fresh is False


def test_jdbc_retriever_oracle_defaults_owner_from_credential(monkeypatch):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchall.return_value = [("EMPLOYEES",)]

    monkeypatch.setenv(
        "TEST_ORACLE_CREDS",
        '{"username": "HR", "password": "secret"}',
    )
    retriever = GenericJdbcRetriever(
        SourceConfig(
            engine="oracle",
            jdbc=JdbcConfig(
                host="localhost",
                port=1521,
                database="ORCL",
                credential_ref="TEST_ORACLE_CREDS",
            ),
        ),
        connection=conn,
    )
    tables = retriever.list_tables("")
    assert tables == ["EMPLOYEES"]
    cursor.execute.assert_called_once()
    assert cursor.execute.call_args[0][1] == ("HR",)


def test_jdbc_retriever_with_fake_cursor():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    cursor.fetchone.return_value = (500.0, 4096)
    cursor.fetchall.side_effect = [
        [("national_id", 1, "varchar", "NO")],
        [("national_id", 120.0, 0.01, 18.0)],
    ]

    retriever = GenericJdbcRetriever(
        SourceConfig(engine="postgres"),
        connection=conn,
    )
    meta = retriever.get_source_metadata("public.customers")

    assert meta.engine == "postgres"
    assert meta.columns[0].name == "national_id"
    assert meta.columns[0].nullable is False
    assert meta.table_props["row_count"] == 500
    assert meta.column_stats["national_id"].ndv == 120
    assert meta.column_stats["national_id"].stats_fresh is True


def test_resolve_credential_ref_from_env(monkeypatch):
    monkeypatch.setenv(
        "TEST_JDBC_CREDS",
        json.dumps({"username": "svc", "password": "secret"}),
    )
    creds = resolve_credential_ref("TEST_JDBC_CREDS")
    assert creds["username"] == "svc"
    assert creds["password"] == "secret"


def test_persisted_objects_never_contain_raw_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TEST_JDBC_CREDS",
        json.dumps({"username": "svc", "password": "super-secret"}),
    )
    cfg = SourceConfig(
        engine="postgres",
        jdbc=JdbcConfig(
            host="localhost",
            port=5432,
            database="db",
            credential_ref="TEST_JDBC_CREDS",
        ),
    )
    dumped = RedibisConfig(source=cfg, memory=MemoryConfig()).to_dict()
    blob = json.dumps(dumped)
    assert "super-secret" not in blob
    assert "TEST_JDBC_CREDS" in blob
