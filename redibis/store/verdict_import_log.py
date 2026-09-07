"""Append-only audit log for verdict-package import/promotion events.

Distinct from ``redibis.evidence.audit`` (which governs *restricted evidence
reads* under the local spool): this is durable provenance for steward
decisions promoted into ``PiiDecisionStore`` from a portable verdict package,
so it lives alongside the other ``_meta/`` sidecars in the contracts bucket —
no new writer to the contracts bucket itself (invariant 1); this only appends
to its own ``_meta/verdict_imports/`` prefix.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from redibis.store.storage_backend import StorageBackend


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerdictImportLog:
    """Reads/writes the per-table verdict-import audit trail.

    Storage layout (in the contracts bucket): ``_meta/verdict_imports/{table}.jsonl``
    """

    PREFIX = "_meta/verdict_imports"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, table: str) -> str:
        return f"{self.PREFIX}/{table}.jsonl"

    def append(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        """Append one import event; returns the record actually written."""
        key = self._key(table)
        existing = ""
        if self.backend.exists(self.bucket, key):
            try:
                existing = self.backend.get_text(self.bucket, key)
            except Exception:
                existing = ""
        full = dict(record)
        full.setdefault("ts", _utc_now_iso())
        full.setdefault("table", table)
        line = json.dumps(full, sort_keys=True, default=str)
        payload = (existing + line + "\n") if existing else (line + "\n")
        self.backend.put_text(self.bucket, key, payload)
        return full

    def list(self, table: str) -> list[dict[str, Any]]:
        """All import events for *table*, oldest first."""
        key = self._key(table)
        if not self.backend.exists(self.bucket, key):
            return []
        text = self.backend.get_text(self.bucket, key)
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out
