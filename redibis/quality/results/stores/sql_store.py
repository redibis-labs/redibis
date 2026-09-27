"""A relational database through SQLAlchemy — Postgres first, any SQLAlchemy URL works.

    postgresql://user:pass@db:5432/quality?schema=dq       (needs psycopg2: pip install "redibis[postgres]")
    sqlite:////tmp/dq.db                                    (tests, single-user)

Tables are created on first use: ``<schema>.partition_runs``, ``rule_results``,
``rule_sets``, with indexes on ``(table_name, partition_date)`` and ``run_id`` —
the lookups consumers do. Superset / Grafana can read them directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

import pandas as pd
import pyarrow as pa

from redibis.quality.results.records import SCHEMAS, TABLES
from redibis.quality.results.store import QualityResultStore, register_store, split_uri


def _column_type(arrow_type: pa.DataType):
    import sqlalchemy as sa

    if pa.types.is_timestamp(arrow_type):
        return sa.DateTime(timezone=True)
    if pa.types.is_date(arrow_type):
        return sa.Date()
    if pa.types.is_boolean(arrow_type):
        return sa.Boolean()
    if pa.types.is_integer(arrow_type):
        return sa.BigInteger()
    return sa.Text()


@register_store("postgresql", "postgres", "sqlite", "mysql")
class SqlResultStore(QualityResultStore):
    def __init__(self, uri: str, *, engine: Any = None, schema: Optional[str] = None, **_options: Any):
        import sqlalchemy as sa

        scheme, _netloc, _path, query = split_uri(uri)
        self.schema = schema or query.pop("schema", None)
        url = uri.split("?", 1)[0]
        if scheme in ("postgres",):
            url = "postgresql" + url[len("postgres"):]
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        self.engine = engine or sa.create_engine(url, future=True)
        self.meta = sa.MetaData(schema=self.schema)
        self.tables = {}
        for name in TABLES:
            cols = [sa.Column(f.name, _column_type(f.type)) for f in SCHEMAS[name]]
            idx = [sa.Index(f"ix_{name}_table_date", "table_name", "partition_date")] \
                if "partition_date" in SCHEMAS[name].names else \
                [sa.Index(f"ix_{name}_table_digest", "table_name", "rule_set_digest")]
            if "run_id" in SCHEMAS[name].names:
                idx.append(sa.Index(f"ix_{name}_run", "run_id"))
            self.tables[name] = sa.Table(name, self.meta, *cols, *idx)
        with self.engine.begin() as conn:
            if self.schema and self.engine.dialect.name == "postgresql":
                conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            self.meta.create_all(conn)
            self._add_new_columns(conn)

    def _add_new_columns(self, conn) -> None:
        """Tables created by an older redibis get the columns added since (NULL for old rows)."""
        import sqlalchemy as sa

        inspector = sa.inspect(conn)
        for name, table in self.tables.items():
            have = {c["name"] for c in inspector.get_columns(name, schema=self.schema)}
            for col in table.columns:
                if col.name not in have:
                    target = f'"{self.schema}"."{name}"' if self.schema else f'"{name}"'
                    kind = col.type.compile(dialect=conn.dialect)
                    conn.execute(sa.text(f'ALTER TABLE {target} ADD COLUMN "{col.name}" {kind}'))

    def _append(self, name: str, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        records = rows.astype(object).where(pd.notna(rows), None).to_dict("records")
        with self.engine.begin() as conn:
            conn.execute(self.tables[name].insert(), records)

    def _read(self, name: str, *, table_name: str, equals: Optional[dict[str, Any]] = None,
              isin: Optional[dict[str, Iterable[Any]]] = None,
              date_from: Optional[date] = None, date_to: Optional[date] = None) -> pd.DataFrame:
        import sqlalchemy as sa

        t = self.tables[name]
        cond = [t.c.table_name == table_name]
        cond += [t.c[k] == v for k, v in (equals or {}).items()]
        cond += [t.c[k].in_(list(v)) for k, v in (isin or {}).items()]
        if date_from is not None:
            cond.append(t.c.partition_date >= date_from)
        if date_to is not None:
            cond.append(t.c.partition_date <= date_to)
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(t).where(sa.and_(*cond))).mappings().all()
        df = pd.DataFrame([dict(r) for r in rows], columns=SCHEMAS[name].names)
        for ts in ("validated_at", "partition_ts"):
            if ts in df and not df.empty:
                df[ts] = pd.to_datetime(df[ts], utc=True)
        if "created_at" in df and not df.empty:
            df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
        return df
