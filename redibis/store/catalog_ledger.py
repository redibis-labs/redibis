"""Catalog projection overlays under ``_meta/`` (ledger, suppressions, cache, audit).

Follows the ``PiiDecisionStore`` pattern. These are overlays — they do **not**
write contract documents (``active/`` / ``audit/``). ``ContractStore.upsert()``
remains the only writer to contract specs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.services.catalog.assertions import (
    AssertionKey,
    LedgerEntry,
    Suppression,
    ledger_entry_from_dict,
    ledger_entry_to_dict,
    suppression_from_dict,
    suppression_to_dict,
)
from redibis.store.storage_backend import StorageBackend


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _key_identity(key: AssertionKey) -> tuple:
    facet = key.facet.value if hasattr(key.facet, "value") else str(key.facet)
    return (key.asset_fqn, key.column_path, facet, key.value_key)


class CatalogLedgerStore:
    """Projection ledger: ``_meta/catalog_ledger/{backend}/{table}.json``."""

    PREFIX = "_meta/catalog_ledger"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, backend: str, table: str) -> str:
        return f"{self.PREFIX}/{backend}/{table}.json"

    def get_entries(self, table: str, backend: str) -> list[LedgerEntry]:
        key = self._key(backend, table)
        if not self.backend.exists(self.bucket, key):
            return []
        raw = self.backend.get_json(self.bucket, key) or {}
        entries = raw.get("entries") or []
        return [ledger_entry_from_dict(e) for e in entries if isinstance(e, dict)]

    def upsert_entries(
        self,
        table: str,
        backend: str,
        entries: list[LedgerEntry],
    ) -> list[LedgerEntry]:
        """Merge ``entries`` into the ledger by assertion key; return full list."""
        by_id = {_key_identity(e.key): e for e in self.get_entries(table, backend)}
        for entry in entries:
            by_id[_key_identity(entry.key)] = entry
        merged = list(by_id.values())
        self.replace_all(table, backend, merged)
        return merged

    def remove_keys(
        self,
        table: str,
        backend: str,
        keys: list[AssertionKey],
    ) -> int:
        """Drop ledger entries matching ``keys``. Returns number removed."""
        drop = {_key_identity(k) for k in keys}
        current = self.get_entries(table, backend)
        kept = [e for e in current if _key_identity(e.key) not in drop]
        removed = len(current) - len(kept)
        if removed:
            self.replace_all(table, backend, kept)
        return removed

    def replace_all(
        self,
        table: str,
        backend: str,
        entries: list[LedgerEntry],
    ) -> None:
        payload = {
            "table": table,
            "backend": backend,
            "updated_at": _utc_now_iso(),
            "entries": [ledger_entry_to_dict(e) for e in entries],
        }
        self.backend.put_json(self.bucket, self._key(backend, table), payload)


class SuppressionStore:
    """Suppressions: ``_meta/catalog_suppressions/{backend}/{table}.json``."""

    PREFIX = "_meta/catalog_suppressions"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, backend: str, table: str) -> str:
        return f"{self.PREFIX}/{backend}/{table}.json"

    def _load(self, table: str, backend: str) -> list[Suppression]:
        key = self._key(backend, table)
        if not self.backend.exists(self.bucket, key):
            return []
        raw = self.backend.get_json(self.bucket, key) or {}
        items = raw.get("suppressions") or []
        return [suppression_from_dict(s) for s in items if isinstance(s, dict)]

    def _save(self, table: str, backend: str, items: list[Suppression]) -> None:
        payload = {
            "table": table,
            "backend": backend,
            "updated_at": _utc_now_iso(),
            "suppressions": [suppression_to_dict(s) for s in items],
        }
        self.backend.put_json(self.bucket, self._key(backend, table), payload)

    def list(
        self,
        table: str,
        backend: str,
        *,
        include_expired: bool = False,
        now: Optional[datetime] = None,
    ) -> list[Suppression]:
        now = now or _utc_now()
        items = self._load(table, backend)
        if include_expired:
            return items
        out: list[Suppression] = []
        for s in items:
            if s.expires_at is None:
                out.append(s)
                continue
            exp = s.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > now:
                out.append(s)
        return out

    def add(self, table: str, backend: str, suppression: Suppression) -> Suppression:
        items = self._load(table, backend)
        identity = _key_identity(suppression.key)
        items = [s for s in items if _key_identity(s.key) != identity]
        items.append(suppression)
        self._save(table, backend, items)
        return suppression

    def remove(self, table: str, backend: str, key: AssertionKey) -> bool:
        items = self._load(table, backend)
        identity = _key_identity(key)
        kept = [s for s in items if _key_identity(s.key) != identity]
        if len(kept) == len(items):
            return False
        self._save(table, backend, kept)
        return True

    def clear(self, table: str, backend: str) -> None:
        key = self._key(backend, table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)


class EntityResolutionCache:
    """Cached FQN resolution: ``_meta/catalog_entities/{backend}/{table}.json``."""

    PREFIX = "_meta/catalog_entities"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, backend: str, table: str) -> str:
        return f"{self.PREFIX}/{backend}/{table}.json"

    def get(self, table: str, backend: str) -> Optional[dict]:
        key = self._key(backend, table)
        if not self.backend.exists(self.bucket, key):
            return None
        raw = self.backend.get_json(self.bucket, key) or {}
        target = raw.get("target_fqn")
        if not target:
            return None
        return {
            "target_fqn": str(target),
            "is_redibis_managed": bool(raw.get("is_redibis_managed", False)),
            "resolved_at": str(raw.get("resolved_at") or ""),
        }

    def put(
        self,
        table: str,
        backend: str,
        *,
        target_fqn: str,
        is_redibis_managed: bool,
        resolved_at: Optional[str] = None,
    ) -> dict:
        payload = {
            "table": table,
            "backend": backend,
            "target_fqn": target_fqn,
            "is_redibis_managed": bool(is_redibis_managed),
            "resolved_at": resolved_at or _utc_now_iso(),
        }
        self.backend.put_json(self.bucket, self._key(backend, table), payload)
        return {
            "target_fqn": payload["target_fqn"],
            "is_redibis_managed": payload["is_redibis_managed"],
            "resolved_at": payload["resolved_at"],
        }

    def invalidate(self, table: str, backend: str) -> None:
        """Drop a cached FQN so the next resolve re-looks up OpenMetadata."""
        key = self._key(backend, table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)


class CatalogAuditStore:
    """Append-only enforce audit: ``_meta/catalog_audit/{backend}/{table}.jsonl``."""

    PREFIX = "_meta/catalog_audit"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, backend: str, table: str) -> str:
        return f"{self.PREFIX}/{backend}/{table}.jsonl"

    def append(self, table: str, backend: str, record: dict) -> None:
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        key = self._key(backend, table)
        try:
            existing = self.backend.get_text(self.bucket, key) or ""
        except Exception:
            existing = ""
        self.backend.put_text(
            self.bucket, key, existing + line, content_type="application/x-ndjson"
        )

    def list_recent(
        self,
        table: str,
        backend: str,
        limit: int = 100,
    ) -> list[dict]:
        key = self._key(backend, table)
        try:
            if not self.backend.exists(self.bucket, key):
                return []
            text = self.backend.get_text(self.bucket, key)
        except Exception:
            return []
        if not text:
            return []
        entries: list[dict] = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
        if limit <= 0:
            return []
        return entries[-limit:]


__all__ = [
    "CatalogLedgerStore",
    "SuppressionStore",
    "EntityResolutionCache",
    "CatalogAuditStore",
]
