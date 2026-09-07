"""
redibis.store.golden_store — durable golden path (source of truth for approved columns).
"""

from __future__ import annotations

import os
from typing import Optional

from redibis.profiling.golden import GoldenColumn
from redibis.store.storage_backend import StorageBackend


class GoldenStore:
    """Sole writer to the golden path. Backends hydrate from here on start."""

    PREFIX = "_meta/golden"

    def __init__(
        self,
        backend: StorageBackend,
        bucket: str,
        prefix: str = PREFIX,
    ):
        self.backend = backend
        self.bucket = bucket
        self.prefix = prefix.strip("/") if prefix else ""

    @classmethod
    def from_env(cls, backend: StorageBackend, contracts_bucket: str) -> GoldenStore:
        golden_bucket = os.getenv("S3_GOLDEN_BUCKET") or os.getenv("S3_METADATA_BUCKET") or contracts_bucket
        if os.getenv("S3_GOLDEN_BUCKET"):
            prefix = ""
        elif os.getenv("S3_METADATA_BUCKET"):
            prefix = cls.PREFIX
        else:
            prefix = cls.PREFIX
        return cls(backend, golden_bucket, prefix=prefix)

    def _key(self, table: str, column: str) -> str:
        base = f"{self.prefix}/{table}" if self.prefix else table
        return f"{base}/{column}.json"

    def write(self, col: GoldenColumn) -> str:
        return self.backend.put_json(self.bucket, self._key(col.table_name, col.column_name), col.to_dict())

    def delete(self, table: str, column: str) -> bool:
        key = self._key(table, column)
        try:
            self.backend.delete(self.bucket, key)
            return True
        except Exception:
            return False

    def list_all(self) -> list[GoldenColumn]:
        prefix = f"{self.prefix}/" if self.prefix else ""
        try:
            keys = self.backend.list_keys(self.bucket, prefix=prefix)
        except Exception:
            return []
        out: list[GoldenColumn] = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            try:
                data = self.backend.get_json(self.bucket, key)
                if isinstance(data, dict):
                    out.append(GoldenColumn.from_dict(data))
            except Exception:
                continue
        return out

    def list_table(self, table: str) -> list[GoldenColumn]:
        base = f"{self.prefix}/{table}" if self.prefix else table
        prefix = f"{base}/"
        try:
            keys = self.backend.list_keys(self.bucket, prefix=prefix)
        except Exception:
            return []
        out: list[GoldenColumn] = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            try:
                data = self.backend.get_json(self.bucket, key)
                if isinstance(data, dict):
                    out.append(GoldenColumn.from_dict(data))
            except Exception:
                continue
        return out

    def get(self, table: str, column: str) -> Optional[GoldenColumn]:
        try:
            data = self.backend.get_json(self.bucket, self._key(table, column))
            return GoldenColumn.from_dict(data) if isinstance(data, dict) else None
        except Exception:
            return None
