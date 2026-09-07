"""
redibis.memory.consent — steward consent for persisting masked/hashed samples.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from redibis.store.storage_backend import StorageBackend


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConsentEntry:
    column: str
    approved: bool = False
    approved_by: str = ""
    approved_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ConsentEntry":
        return cls(
            column=d["column"],
            approved=bool(d.get("approved", False)),
            approved_by=d.get("approved_by", ""),
            approved_at=d.get("approved_at") or _utc_now_iso(),
        )


class SamplingConsentStore:
    """Per-table sampling consent overlay under ``_meta/sampling_consent/``."""

    PREFIX = "_meta/sampling_consent"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, table: str) -> str:
        return f"{self.PREFIX}/{table}.json"

    def _load(self, table: str) -> dict[str, dict]:
        key = self._key(table)
        if not self.backend.exists(self.bucket, key):
            return {}
        raw = self.backend.get_json(self.bucket, key)
        return raw.get("columns") or {}

    def is_approved(self, table: str, column: str) -> bool:
        entry = self._load(table).get(column) or {}
        return bool(entry.get("approved"))

    def set_approved(
        self,
        table: str,
        column: str,
        *,
        approved: bool,
        approved_by: str = "",
    ) -> dict:
        columns = self._load(table)
        entry = ConsentEntry(
            column=column,
            approved=approved,
            approved_by=approved_by,
            approved_at=_utc_now_iso(),
        )
        columns[column] = entry.to_dict()
        self.backend.put_json(
            self.bucket,
            self._key(table),
            {"table": table, "columns": columns},
        )
        return entry.to_dict()

    def list(self, table: str) -> list[dict]:
        return list(self._load(table).values())

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)
