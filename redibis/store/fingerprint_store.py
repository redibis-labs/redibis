"""
redibis.store.fingerprint_store — Tier A object store for ALL scanned column fingerprints.
"""

from __future__ import annotations

import os
from typing import Optional

from redibis.profiling.fingerprint import ColumnFingerprint
from redibis.store.storage_backend import StorageBackend


class FingerprintStore:
    """Sole writer to the fingerprint prefix in object storage."""

    PREFIX = "_meta/fingerprints"

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
    def from_env(cls, backend: StorageBackend, contracts_bucket: str) -> FingerprintStore:
        meta_bucket = os.getenv("S3_METADATA_BUCKET") or contracts_bucket
        prefix = "" if os.getenv("S3_METADATA_BUCKET") else cls.PREFIX
        return cls(backend, meta_bucket, prefix=prefix)

    def _key(self, table: str, column: str) -> str:
        base = f"{self.prefix}/{table}" if self.prefix else table
        return f"{base}/{column}.json"

    def write_column(self, table: str, fp: ColumnFingerprint) -> str:
        key = self._key(table, fp.column)
        return self.backend.put_json(self.bucket, key, fp.to_dict())

    def write_table(self, table: str, fps: list[ColumnFingerprint]) -> None:
        for fp in fps:
            self.write_column(table, fp)

    def get_column(self, table: str, column: str) -> Optional[dict]:
        try:
            return self.backend.get_json(self.bucket, self._key(table, column))
        except Exception:
            return None

    def list_table(self, table: str) -> list[dict]:
        base = f"{self.prefix}/{table}" if self.prefix else table
        prefix = f"{base}/"
        try:
            keys = self.backend.list_keys(self.bucket, prefix=prefix)
        except Exception:
            return []
        out: list[dict] = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            try:
                data = self.backend.get_json(self.bucket, key)
                if isinstance(data, dict):
                    out.append(data)
            except Exception:
                continue
        return out
