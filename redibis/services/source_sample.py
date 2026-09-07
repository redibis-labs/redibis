"""Framework-neutral bounded sample loading from external sources."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from redibis.config import ConfigError, SourceConfig


def load_table_sample(
    table: str,
    *,
    source_config: Optional[SourceConfig] = None,
    file_path: Optional[str | Path] = None,
    rows: int = 5000,
    spark: Any = None,
) -> pd.DataFrame:
    """
    Load a bounded sample for ``table``.

  Priority:
    1. Explicit ``file_path`` (CSV/Parquet)
    2. ``source_config`` engine (hive / jdbc / postgres / oracle)
    """
    if file_path is not None:
        return _load_file_sample(file_path, rows=rows)

    if source_config is None:
        raise ConfigError("Provide file_path or source_config for sampling")

    engine = (source_config.engine or "none").lower()
    if engine == "hive":
        return _load_hive_sample(table, rows=rows, spark=spark)
    if engine in ("oracle", "postgres", "jdbc"):
        return _load_jdbc_sample(source_config, table, rows=rows)
    if engine in ("local", "none", ""):
        raise ConfigError(
            "source.engine is local/none — pass --sample or --sample-dir for monitor runs"
        )
    raise ConfigError(
        f"unsupported source engine {source_config.engine!r} for monitor sampling"
    )


def _load_file_sample(path: str | Path, *, rows: int) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"sample file not found: {p}")
    limit = max(1, min(int(rows), 100_000))
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(p).head(limit)
    if suffix in (".csv", ".tsv", ".txt"):
        sep = "\t" if suffix == ".tsv" else ","
        return pd.read_csv(p, nrows=limit, sep=sep)
    raise ConfigError(f"unsupported sample file type: {suffix}")


def _load_hive_sample(table: str, *, rows: int, spark: Any) -> pd.DataFrame:
    from redibis.metadata.dialect import split_qualified_table

    if spark is None:
        raise ConfigError(
            "Hive sampling requires Spark — inject via source or --spark"
        )
    limit = max(1, min(int(rows), 100_000))
    split_qualified_table(table)
    return spark.table(table).limit(limit).toPandas()


def _load_jdbc_sample(source_config: SourceConfig, table: str, *, rows: int) -> pd.DataFrame:
    from redibis.metadata.credentials import resolve_credential_ref
    from redibis.metadata.dialect import bounded_select_sql

    engine = (source_config.engine or "jdbc").lower()
    jdbc = source_config.jdbc
    if not jdbc.host:
        raise ConfigError("jdbc.host is required for JDBC sampling")

    creds = resolve_credential_ref(jdbc.credential_ref)
    user = creds.get("username") or creds.get("user") or ""
    password = creds.get("password") or ""
    limit = max(1, min(int(rows), 100_000))
    sql = bounded_select_sql(engine, table=table, rows=limit)
    conn = _open_jdbc_connection(engine, jdbc, user=user, password=password)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*SQLAlchemy connectable.*",
                category=UserWarning,
            )
            return pd.read_sql(sql, conn)
    finally:
        conn.close()


def _open_jdbc_connection(engine: str, jdbc: Any, *, user: str, password: str) -> Any:
    if engine == "postgres":
        import psycopg2  # type: ignore[import-untyped]

        return psycopg2.connect(
            host=jdbc.host,
            port=jdbc.port or 5432,
            dbname=jdbc.database,
            user=user,
            password=password,
        )

    if engine == "oracle":
        import oracledb  # type: ignore[import-untyped]

        dsn = f"{jdbc.host}:{jdbc.port or 1521}/{jdbc.database}"
        return oracledb.connect(user=user, password=password, dsn=dsn)

    raise ConfigError(
        f"JDBC sampling for engine {engine!r} requires postgres or oracle driver"
    )
