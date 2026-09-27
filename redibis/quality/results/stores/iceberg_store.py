"""Apache Iceberg tables through PyIceberg — the default store.

    iceberg://<catalog>?namespace=dq                 catalog configured in ~/.pyiceberg.yaml
                                                     or PYICEBERG_CATALOG__<CATALOG>__* env vars
    iceberg://prod?namespace=dq&type=rest&uri=http://catalog:8181&warehouse=s3://lake/
    iceberg://prod?namespace=dq&type=glue            (any catalog PyIceberg supports:
                                                      REST, Hive, Glue, SQL, …)

Tables ``<namespace>.partition_runs``, ``rule_results`` and ``rule_sets`` are
created on first use, partitioned by ``table_name``. They are ordinary Iceberg
tables: Spark, Trino and Superset query them directly
(``SELECT … FROM prod.dq.rule_results WHERE table_name = 'shop.orders'``).
MinIO works through the catalog's ``s3.endpoint`` property.

Needs ``pip install "redibis[iceberg]"``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

import pandas as pd
import pyarrow as pa

from redibis.quality.results.records import SCHEMAS, TABLES
from redibis.quality.results.store import QualityResultStore, register_store, split_uri


@register_store("iceberg")
class IcebergResultStore(QualityResultStore):
    def __init__(self, uri: str, *, catalog: Any = None, namespace: Optional[str] = None,
                 **options: Any):
        try:
            from pyiceberg.catalog import load_catalog
        except ImportError as exc:  # pragma: no cover — depends on the install
            raise ImportError('the Iceberg result store needs PyIceberg: pip install "redibis[iceberg]" '
                              "(or choose s3://, file:// or postgresql://)") from exc
        _scheme, name, _path, query = split_uri(uri)
        self.namespace = namespace or query.pop("namespace", "dq")
        props = {**query, **{k: str(v) for k, v in options.items()}}
        self.catalog = catalog or load_catalog(name or "default", **props)
        try:
            self.catalog.create_namespace(self.namespace)
        except Exception:  # noqa: BLE001 — already exists (exception type differs per catalog)
            pass
        self.tables = {name: self._table(name) for name in TABLES}

    def _table(self, name: str):
        from pyiceberg.exceptions import NoSuchTableError

        ident = (self.namespace, name)
        try:
            table = self.catalog.load_table(ident)
        except NoSuchTableError:
            table = None
        if table is not None:
            have = {f.name for f in table.schema().fields}
            if set(SCHEMAS[name].names) - have:          # created by an older redibis
                with table.update_schema() as update:
                    update.union_by_name(SCHEMAS[name])
                table = self.catalog.load_table(ident)
            return table
        table = self.catalog.create_table(ident, schema=SCHEMAS[name])
        try:
            with table.update_spec() as update:
                update.add_identity("table_name")
        except Exception:  # noqa: BLE001 — unpartitioned still works, it just scans more
            pass
        return self.catalog.load_table(ident)

    def _append(self, name: str, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        table = self.tables[name]
        arrow = pa.Table.from_pandas(rows, schema=SCHEMAS[name], preserve_index=False)
        arrow = arrow.select([f.name for f in table.schema().fields])   # evolved tables: their column order
        table.append(arrow)
        self.tables[name] = table.refresh()

    def _read(self, name: str, *, table_name: str, equals: Optional[dict[str, Any]] = None,
              isin: Optional[dict[str, Iterable[Any]]] = None,
              date_from: Optional[date] = None, date_to: Optional[date] = None) -> pd.DataFrame:
        from pyiceberg.expressions import And, EqualTo, GreaterThanOrEqual, In, LessThanOrEqual

        expr = EqualTo("table_name", table_name)
        for k, v in (equals or {}).items():
            expr = And(expr, EqualTo(k, v))
        for k, values in (isin or {}).items():
            values = list(values)
            if not values:
                return pd.DataFrame(columns=SCHEMAS[name].names)
            expr = And(expr, In(k, values))
        if date_from is not None:
            expr = And(expr, GreaterThanOrEqual("partition_date", date_from.isoformat()))
        if date_to is not None:
            expr = And(expr, LessThanOrEqual("partition_date", date_to.isoformat()))
        table = self.tables[name].refresh()
        return table.scan(row_filter=expr).to_arrow().to_pandas()
