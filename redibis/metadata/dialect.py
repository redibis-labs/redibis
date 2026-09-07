"""
redibis.metadata.dialect
========================
Shared SQL dialect helper for pushdown aggregate queries (Tier B).
"""

from __future__ import annotations

import re

from redibis.config import ConfigError

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_AGG_OPS = frozenset({
    "count_distinct",
    "approx_distinct",
    "null_count",
    "min_max",
})

_HIVE_SQL = {
    "count_distinct": "COUNT(DISTINCT `{column}`)",
    "approx_distinct": "APPROX_COUNT_DISTINCT(`{column}`)",
    "null_count": "SUM(CASE WHEN `{column}` IS NULL THEN 1 ELSE 0 END)",
    "min_max": "MIN(`{column}`), MAX(`{column}`)",
}

_POSTGRES_SQL = {
    "count_distinct": 'COUNT(DISTINCT "{column}")',
    "approx_distinct": 'COUNT(DISTINCT "{column}")',
    "null_count": 'COUNT(*) FILTER (WHERE "{column}" IS NULL)',
    "min_max": 'MIN("{column}"), MAX("{column}")',
}

_ORACLE_SQL = {
    "count_distinct": 'COUNT(DISTINCT "{column}")',
    "approx_distinct": 'APPROX_COUNT_DISTINCT("{column}")',
    "null_count": 'SUM(CASE WHEN "{column}" IS NULL THEN 1 ELSE 0 END)',
    "min_max": 'MIN("{column}"), MAX("{column}")',
}

_SQLITE_SQL = {
    "count_distinct": 'COUNT(DISTINCT "{column}")',
    "approx_distinct": 'COUNT(DISTINCT "{column}")',
    "null_count": 'SUM(CASE WHEN "{column}" IS NULL THEN 1 ELSE 0 END)',
    "min_max": 'MIN("{column}"), MAX("{column}")',
}

_JDBC_SQL = _POSTGRES_SQL


def validate_sql_identifier(name: str, *, label: str = "identifier") -> str:
    """Reject identifiers that could break quoted SQL (user/catalog input)."""
    n = (name or "").strip()
    if not n or not _IDENTIFIER_RE.match(n):
        raise ConfigError(
            f"invalid SQL {label} {name!r}: "
            "use letters, digits, underscore, or $ only (no spaces or quotes)"
        )
    return n


def split_qualified_table(table: str) -> tuple[str, str]:
    """Split ``schema.table`` and validate each identifier segment."""
    raw = (table or "").strip()
    if not raw:
        raise ConfigError("table name is required")
    if "." in raw:
        db, tbl = raw.split(".", 1)
        return (
            validate_sql_identifier(db, label="schema"),
            validate_sql_identifier(tbl, label="table"),
        )
    return "", validate_sql_identifier(raw, label="table")


def aggregate_sql(
    engine: str,
    op: str,
    *,
    table: str,
    column: str,
    approx_distinct: bool = True,
) -> str:
    """
    Build a single-column aggregate SELECT for ``op``.

    ``op`` is one of: count_distinct, approx_distinct, null_count, min_max.
    """
    engine_key = (engine or "jdbc").lower()
    op_key = (op or "").lower()
    if op_key not in _AGG_OPS:
        raise ConfigError(
            f"unknown aggregate op {op!r}; choices: {sorted(_AGG_OPS)}"
        )

    if op_key == "approx_distinct" and not approx_distinct:
        op_key = "count_distinct"

    templates = {
        "hive": _HIVE_SQL,
        "postgres": _POSTGRES_SQL,
        "oracle": _ORACLE_SQL,
        "sqlite": _SQLITE_SQL,
        "jdbc": _JDBC_SQL,
    }.get(engine_key, _JDBC_SQL)

    safe_column = validate_sql_identifier(column, label="column")
    expr = templates[op_key].format(column=safe_column)
    db, tbl = split_qualified_table(table)
    if engine_key == "hive":
        qualified = f"`{db}`.`{tbl}`" if db else f"`{tbl}`"
    elif engine_key == "oracle":
        qualified = f'"{db}"."{tbl}"' if db else f'"{tbl}"'
    else:
        qualified = f'"{db}"."{tbl}"' if db else f'"{tbl}"'

    return f"SELECT {expr} FROM {qualified}"


def bounded_select_sql(engine: str, *, table: str, rows: int) -> str:
    """Dialect-aware bounded ``SELECT *`` for external sample loading."""
    engine_key = (engine or "jdbc").lower()
    db, tbl = split_qualified_table(table)
    n = max(1, int(rows))

    if engine_key == "hive":
        qualified = f"`{db}`.`{tbl}`" if db else f"`{tbl}`"
        return f"SELECT * FROM {qualified} LIMIT {n}"

    if engine_key == "oracle":
        qualified = f'"{db}"."{tbl}"' if db else f'"{tbl}"'
        return f"SELECT * FROM {qualified} FETCH FIRST {n} ROWS ONLY"

    qualified = f'"{db}"."{tbl}"' if db else f'"{tbl}"'
    return f"SELECT * FROM {qualified} LIMIT {n}"
