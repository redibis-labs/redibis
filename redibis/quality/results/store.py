"""Where quality results live: one interface, one implementation per storage.

The **strategy**: every store implements two primitives — append rows to a
logical table, and read rows back with simple filters — and inherits the rest
(idempotent writes, run lookups, rule sets) from :class:`QualityResultStore`.

    get_result_store("iceberg://prod?namespace=dq")          # default: Iceberg (PyIceberg)
    get_result_store("s3://dq-results/quality?endpoint=http://minio:9000")   # Parquet on S3 / MinIO
    get_result_store("file:///data/dq")                       # Parquet on a local/NFS path
    get_result_store("postgresql://dq@db:5432/quality?schema=dq")            # Postgres (any SQLAlchemy URL)

New backends register a URI scheme with :func:`register_store`.
With no URI, ``REDIBIS_QUALITY_RESULTS_STORE`` is used, else ``iceberg://default``.
"""

from __future__ import annotations

import abc
import os
from datetime import date
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlsplit

import pandas as pd

from redibis.quality.results.records import (
    PARTITION_RUNS,
    RULE_RESULTS,
    RULE_SETS,
    SCHEMAS,
    PartitionRun,
)

DEFAULT_STORE_URI = "iceberg://default?namespace=dq"
ENV_STORE_URI = "REDIBIS_QUALITY_RESULTS_STORE"


class QualityResultStore(abc.ABC):
    """Append-only storage for quality runs, results and rule sets."""

    uri: str = ""

    # ── primitives each backend implements ────────────────────────────────

    @abc.abstractmethod
    def _append(self, name: str, rows: pd.DataFrame) -> None:
        """Append ``rows`` (columns = ``SCHEMAS[name]``) to logical table ``name``."""

    @abc.abstractmethod
    def _read(self, name: str, *, table_name: str, equals: Optional[dict[str, Any]] = None,
              isin: Optional[dict[str, Iterable[Any]]] = None,
              date_from: Optional[date] = None, date_to: Optional[date] = None) -> pd.DataFrame:
        """Rows of ``name`` for one data table, filtered (dates on ``partition_date``)."""

    # ── shared behaviour ─────────────────────────────────────────────────

    @staticmethod
    def _frame(name: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
        cols = SCHEMAS[name].names
        return pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)

    def find_run(self, table_name: str, partition: str, data_fingerprint: str,
                 rule_set_digest: str) -> Optional[PartitionRun]:
        """The run that already validated this exact data with these exact rules."""
        df = self._read(PARTITION_RUNS, table_name=table_name,
                        equals={"partition": partition, "data_fingerprint": data_fingerprint,
                                "rule_set_digest": rule_set_digest})
        if df.empty:
            return None
        row = df.sort_values("validated_at").iloc[-1].to_dict()
        return PartitionRun.from_row(row, skipped=True)

    def save_rule_set(self, table_name: str, rule_set_digest: str, rows: list[dict[str, Any]]) -> bool:
        """Store a rule set once per digest. Returns False when it was already there."""
        if not self._read(RULE_SETS, table_name=table_name,
                          equals={"rule_set_digest": rule_set_digest}).empty:
            return False
        self._append(RULE_SETS, self._frame(RULE_SETS, rows))
        return True

    def save_run(self, run: PartitionRun, results: list[dict[str, Any]]) -> None:
        """Write a run's results, then the run row (a reader never sees a run without results)."""
        if results:
            self._append(RULE_RESULTS, self._frame(RULE_RESULTS, results))
        self._append(PARTITION_RUNS, self._frame(PARTITION_RUNS, [run.row()]))

    def runs(self, table_name: str, *, partitions: Optional[Iterable[str]] = None,
             date_from: Optional[date] = None, date_to: Optional[date] = None,
             latest_only: bool = False) -> pd.DataFrame:
        """Validation runs of a table (optionally one per partition: the latest)."""
        isin = {"partition": list(partitions)} if partitions is not None else None
        df = self._read(PARTITION_RUNS, table_name=table_name, isin=isin,
                        date_from=date_from, date_to=date_to)
        if df.empty:
            return df
        df = df.sort_values(["partition", "validated_at"])
        if latest_only:
            df = df.groupby("partition", as_index=False).tail(1)
        return df.reset_index(drop=True)

    def latest(self, table_name: str) -> tuple[Optional[PartitionRun], pd.DataFrame]:
        """The most recent partition's latest run, and its rule results.

        "Most recent" is ``max(partition_ts)`` (then ``partition_date``), and within
        that partition ``max(validated_at)`` — the same as this SQL on any store::

            SELECT * FROM rule_results r
            WHERE  r.table_name = 'shop.orders'
            AND    r.run_id = (SELECT run_id FROM partition_runs
                               WHERE table_name = 'shop.orders'
                               ORDER BY partition_ts DESC NULLS LAST, partition_date DESC NULLS LAST,
                                        validated_at DESC
                               LIMIT 1)
        """
        runs = self._read(PARTITION_RUNS, table_name=table_name)
        if runs.empty:
            return None, pd.DataFrame(columns=SCHEMAS[RULE_RESULTS].names)
        order = runs.assign(
            _ts=pd.to_datetime(runs["partition_ts"], utc=True, errors="coerce"),
            _d=pd.to_datetime(runs["partition_date"], errors="coerce"),
            _v=pd.to_datetime(runs["validated_at"], utc=True, errors="coerce"),
        ).sort_values(["_ts", "_d", "_v"], na_position="first")
        run = PartitionRun.from_row(order.iloc[-1].drop(["_ts", "_d", "_v"]).to_dict())
        return run, self.results(table_name, [run.run_id])

    def results(self, table_name: str, run_ids: Iterable[str], *,
                rule_ids: Optional[Iterable[str]] = None) -> pd.DataFrame:
        isin: dict[str, Iterable[Any]] = {"run_id": list(run_ids)}
        if rule_ids is not None:
            isin["rule_id"] = list(rule_ids)
        return self._read(RULE_RESULTS, table_name=table_name, isin=isin)

    def rule_set(self, table_name: str, rule_set_digest: Optional[str] = None) -> pd.DataFrame:
        """Rules of one digest, or of the most recently stored rule set when omitted."""
        equals = {"rule_set_digest": rule_set_digest} if rule_set_digest else None
        df = self._read(RULE_SETS, table_name=table_name, equals=equals)
        if df.empty or rule_set_digest:
            return df.reset_index(drop=True)
        latest = df.sort_values("created_at").iloc[-1]["rule_set_digest"]
        return df[df["rule_set_digest"] == latest].reset_index(drop=True)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.uri!r})"


# ── registry (the strategy lookup) ────────────────────────────────────────────

_REGISTRY: dict[str, Callable[..., QualityResultStore]] = {}


def register_store(*schemes: str) -> Callable:
    """Class decorator: ``@register_store("s3", "minio")`` makes ``s3://…`` resolve to it."""
    def deco(factory: Callable[..., QualityResultStore]):
        for s in schemes:
            _REGISTRY[s] = factory
        return factory
    return deco


def split_uri(uri: str) -> tuple[str, str, str, dict[str, str]]:
    """``scheme://netloc/path?k=v`` → ``(scheme, netloc, path, options)``."""
    parts = urlsplit(uri)
    return parts.scheme.lower(), parts.netloc, parts.path, dict(parse_qsl(parts.query))


def get_result_store(uri: Optional[str] = None, **options: Any) -> QualityResultStore:
    """Resolve a store from a URI (see module docstring). Keyword options override the URI's."""
    from redibis.quality.results import stores  # noqa: F401 — registers the built-in backends

    uri = uri or os.environ.get(ENV_STORE_URI) or DEFAULT_STORE_URI
    scheme = split_uri(uri)[0].split("+", 1)[0]
    factory = _REGISTRY.get(scheme)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"no quality result store for {scheme!r} (known: {known})")
    store = factory(uri, **options)
    store.uri = uri
    return store
