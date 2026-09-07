"""Tests for external sample loading (C3b)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.agents.external_sample import load_external_sample
from redibis.config import ConfigError, JdbcConfig, SourceConfig
from redibis.metadata.dialect import aggregate_sql, bounded_select_sql


def test_bounded_select_sql_oracle():
    sql = bounded_select_sql("oracle", table="HR.EMPLOYEES", rows=100)
    assert "FETCH FIRST 100 ROWS ONLY" in sql
    assert "HR" in sql and "EMPLOYEES" in sql


def test_bounded_select_sql_postgres():
    sql = bounded_select_sql("postgres", table="public.customers", rows=50)
    assert "LIMIT 50" in sql
    assert "public" in sql


def test_bounded_select_sql_rejects_injection():
    with pytest.raises(ConfigError, match="invalid SQL"):
        bounded_select_sql("postgres", table='x" UNION SELECT 1', rows=10)


def test_aggregate_sql_rejects_bad_column():
    with pytest.raises(ConfigError, match="invalid SQL"):
        aggregate_sql("postgres", "count_distinct", table="public.t", column="col;drop")


def test_load_hive_sample_requires_spark():
    with pytest.raises(ConfigError, match="requires Spark"):
        load_external_sample(SourceConfig(engine="hive"), "db.t", rows=10, spark=None)


def test_load_hive_sample_with_spark():
    spark = MagicMock()
    pdf = pd.DataFrame({"a": [1, 2]})
    spark.table.return_value.limit.return_value.toPandas.return_value = pdf
    out = load_external_sample(SourceConfig(engine="hive"), "db.t", rows=10, spark=spark)
    spark.table.assert_called_once_with("db.t")
    spark.table.return_value.limit.assert_called_once_with(10)
    assert len(out) == 2


@patch("redibis.services.source_sample.pd.read_sql")
@patch("redibis.metadata.credentials.resolve_credential_ref")
@patch("redibis.services.source_sample._open_jdbc_connection")
def test_load_oracle_jdbc_sample(mock_conn_fn, mock_creds, mock_read_sql):
    mock_creds.return_value = {"username": "u", "password": "p"}
    conn = MagicMock()
    mock_conn_fn.return_value = conn
    mock_read_sql.return_value = pd.DataFrame({"id": [1]})

    cfg = SourceConfig(
        engine="oracle",
        jdbc=JdbcConfig(host="host", port=1521, database="ORCL", credential_ref="REF"),
    )
    out = load_external_sample(cfg, "HR.EMPLOYEES", rows=25)
    assert len(out) == 1
    mock_read_sql.assert_called_once()
    sql = mock_read_sql.call_args[0][0]
    assert "FETCH FIRST 25 ROWS ONLY" in sql
    conn.close.assert_called_once()
