"""Parquet files on a local/NFS path or an S3-compatible object store (S3, MinIO, SeaweedFS).

    file:///data/dq
    s3://dq-results/quality                                  (AWS; credentials from the environment)
    s3://dq-results/quality?endpoint=http://minio:9000       (MinIO; also minio://dq-results/quality)

Layout (Hive-style, readable by Spark, Trino, DuckDB)::

    <root>/partition_runs/table_name=<table>/<run_id>.parquet
    <root>/rule_results/table_name=<table>/<run_id>.parquet
    <root>/rule_sets/table_name=<table>/<rule_set_digest>.parquet

Credentials: ``S3_ACCESS_KEY``/``S3_SECRET_KEY`` (or ``AWS_ACCESS_KEY_ID``/
``AWS_SECRET_ACCESS_KEY``); endpoint from ``?endpoint=`` or ``S3_ENDPOINT_URL``.
Files are written once and never rewritten, so concurrent writers never clash.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from typing import Any, Iterable, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from redibis.quality.results.records import RULE_SETS, SCHEMAS
from redibis.quality.results.store import QualityResultStore, register_store, split_uri
from redibis.quality.results.stores import _filters


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value))


@register_store("file", "s3", "s3a", "minio")
class ObjectResultStore(QualityResultStore):
    def __init__(self, uri: str, *, filesystem: Optional[pafs.FileSystem] = None, **options: Any):
        scheme, netloc, path, query = split_uri(uri)
        query.update({k: str(v) for k, v in options.items()})
        if filesystem is not None:
            self.fs, self.root = filesystem, (netloc + path).rstrip("/")
        elif scheme == "file":
            self.fs, self.root = pafs.LocalFileSystem(), os.path.abspath(netloc + path)
        else:
            endpoint = query.get("endpoint") or os.environ.get("S3_ENDPOINT_URL") or None
            scheme_http = "http"
            if endpoint and "://" in endpoint:
                scheme_http, endpoint = endpoint.split("://", 1)
            self.fs = pafs.S3FileSystem(
                access_key=os.environ.get("S3_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID"),
                secret_key=os.environ.get("S3_SECRET_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY"),
                endpoint_override=endpoint,
                scheme=scheme_http if endpoint else "https",
                region=query.get("region") or os.environ.get("S3_REGION") or "us-east-1",
            )
            self.root = f"{netloc}{path}".rstrip("/")

    def _dir(self, name: str, table_name: str) -> str:
        return f"{self.root}/{name}/table_name={_safe(table_name)}"

    def _append(self, name: str, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        for table_name, part in rows.groupby("table_name"):
            folder = self._dir(name, str(table_name))
            self.fs.create_dir(folder, recursive=True)
            leaf = (str(part["rule_set_digest"].iloc[0]) if name == RULE_SETS
                    else str(part["run_id"].iloc[0]) + "-" + uuid.uuid4().hex[:6])
            table = pa.Table.from_pandas(part, schema=SCHEMAS[name], preserve_index=False)
            pq.write_table(table, f"{folder}/{_safe(leaf)}.parquet", filesystem=self.fs)

    def _read(self, name: str, *, table_name: str, equals: Optional[dict[str, Any]] = None,
              isin: Optional[dict[str, Iterable[Any]]] = None,
              date_from: Optional[date] = None, date_to: Optional[date] = None) -> pd.DataFrame:
        folder = self._dir(name, table_name)
        info = self.fs.get_file_info(folder)
        if info.type != pafs.FileType.Directory:
            return pd.DataFrame(columns=SCHEMAS[name].names)
        files = [f.path for f in self.fs.get_file_info(pafs.FileSelector(folder))
                 if f.type == pafs.FileType.File and f.path.endswith(".parquet")]
        if not files:
            return pd.DataFrame(columns=SCHEMAS[name].names)
        frames = [pq.read_table(f, filesystem=self.fs, schema=SCHEMAS[name]).to_pandas() for f in files]
        df = pd.concat(frames, ignore_index=True)
        return _filters.apply(df, table_name=table_name, equals=equals, isin=isin,
                              date_from=date_from, date_to=date_to)
