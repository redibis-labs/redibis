"""OpenMetadata change-event feedback → suppressions (human wins across scans)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from redibis.services.catalog.assertions import (
    AssertionKey,
    Facet,
    Suppression,
)
from redibis.store.catalog_ledger import CatalogLedgerStore, SuppressionStore
from redibis.store.storage_backend import StorageBackend

logger = logging.getLogger(__name__)

_FEEDBACK_PREFIX = "_meta/catalog_feedback"
_REDIBIS_TAG_PREFIXES = ("PII.", "Redibis.", "RedibisPolicy.")


class _FeedbackClient(Protocol):
    def get_events(
        self,
        *,
        entity_type: str = "table",
        timestamp_ms: int = 0,
    ) -> list[dict]: ...


def _tag_facet(tag_fqn: str) -> Facet:
    if tag_fqn.startswith("PII."):
        return Facet.PII_TAG
    if tag_fqn.startswith("RedibisPolicy."):
        return Facet.POLICY_TAG
    return Facet.ENTITY_TAG


def _is_redibis_tag(tag_fqn: str) -> bool:
    return any(tag_fqn.startswith(p) for p in _REDIBIS_TAG_PREFIXES)


def _feedback_key(backend: str) -> str:
    return f"{_FEEDBACK_PREFIX}/{backend}.json"


def load_feedback_cursor(
    storage: StorageBackend,
    bucket: str,
    backend: str,
) -> int:
    key = _feedback_key(backend)
    if not storage.exists(bucket, key):
        return 0
    raw = storage.get_json(bucket, key) or {}
    try:
        return int(raw.get("last_seen_ms") or 0)
    except (TypeError, ValueError):
        return 0


def save_feedback_cursor(
    storage: StorageBackend,
    bucket: str,
    backend: str,
    last_seen_ms: int,
) -> None:
    storage.put_json(
        bucket,
        _feedback_key(backend),
        {
            "backend": backend,
            "last_seen_ms": int(last_seen_ms),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _table_from_fqn(fqn: str) -> str:
    """Best-effort ``db.table`` from an OM table FQN (service.db.schema.table)."""
    parts = [p for p in (fqn or "").split(".") if p]
    if len(parts) >= 2:
        return f"{parts[-3] if len(parts) >= 3 else parts[-2]}.{parts[-1]}"
    return fqn


def _deleted_tags(change: dict) -> list[tuple[str, str]]:
    """Return list of (column_path, tag_fqn) from fieldsDeleted."""
    out: list[tuple[str, str]] = []
    for item in change.get("fieldsDeleted") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name != "tags" and not name.endswith(".tags"):
            # OM sometimes nests as columns/msisdn/tags
            if "tags" not in name:
                continue
        old_value = item.get("oldValue")
        # oldValue may be a tag object, list, or JSON string
        tags: list = []
        if isinstance(old_value, list):
            tags = old_value
        elif isinstance(old_value, dict):
            tags = [old_value]
        elif isinstance(old_value, str) and old_value.strip()[:1] in "{[":
            import json
            try:
                parsed = json.loads(old_value)
                tags = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                tags = []

        column_path = ""
        # Extract column from field name like "columns.msisdn.tags"
        parts = name.split(".")
        if "columns" in parts:
            try:
                ci = parts.index("columns")
                if ci + 1 < len(parts):
                    column_path = parts[ci + 1]
            except ValueError:
                pass

        for tag in tags:
            if not isinstance(tag, dict):
                continue
            tag_fqn = str(tag.get("tagFQN") or "").strip()
            if tag_fqn and _is_redibis_tag(tag_fqn):
                out.append((column_path, tag_fqn))
    return out


def sync_feedback(
    client: _FeedbackClient,
    *,
    backend: str = "openmetadata",
    table: Optional[str] = None,
    last_seen_ms: Optional[int] = None,
    suppression_store: Optional[SuppressionStore] = None,
    ledger_store: Optional[CatalogLedgerStore] = None,
    storage: Optional[StorageBackend] = None,
    bucket: Optional[str] = None,
) -> dict[str, Any]:
    """Poll OM change events and record suppressions for deleted Redibis tags.

    Persist ``last_seen_ms`` under ``_meta/catalog_feedback/{backend}.json`` when
    ``storage`` + ``bucket`` are provided.
    """
    cursor = last_seen_ms
    if cursor is None and storage is not None and bucket:
        cursor = load_feedback_cursor(storage, bucket, backend)
    cursor = int(cursor or 0)

    events = client.get_events(entity_type="table", timestamp_ms=cursor)
    suppressions_written = 0
    ledger_dropped = 0
    max_ts = cursor
    now = datetime.now(timezone.utc)

    for event in events:
        if not isinstance(event, dict):
            continue
        ts = event.get("timestamp") or event.get("eventTime") or 0
        try:
            ts_i = int(ts)
        except (TypeError, ValueError):
            ts_i = 0
        if ts_i > max_ts:
            max_ts = ts_i

        if str(event.get("eventType") or "") not in (
            "entityUpdated",
            "ENTITY_UPDATED",
            "entity_updated",
        ):
            continue

        entity = event.get("entity") or {}
        if isinstance(entity, str):
            entity_fqn = entity
        else:
            entity_fqn = str(
                entity.get("fullyQualifiedName") or entity.get("name") or ""
            )
        physical = _table_from_fqn(entity_fqn)
        if table and physical != table and entity_fqn != table:
            # Also allow exact match on full FQN when filtering
            if table not in (physical, entity_fqn):
                continue

        change = event.get("changeDescription") or {}
        user = event.get("user")
        if isinstance(user, dict) and user.get("name"):
            actor = str(user["name"])
        else:
            actor = str(event.get("userName") or "unknown")

        for column_path, tag_fqn in _deleted_tags(change):
            key = AssertionKey(
                asset_fqn=entity_fqn or physical,
                column_path=column_path,
                facet=_tag_facet(tag_fqn),
                value_key=tag_fqn,
            )
            if suppression_store is not None:
                suppression_store.add(
                    physical or table or entity_fqn,
                    backend,
                    Suppression(
                        key=key,
                        actor=actor,
                        reason=f"tag deleted in OpenMetadata ({tag_fqn})",
                        created_at=now,
                        source="om_change_event",
                    ),
                )
                suppressions_written += 1
            if ledger_store is not None:
                dropped = ledger_store.remove_keys(
                    physical or table or entity_fqn,
                    backend,
                    [key],
                )
                ledger_dropped += int(dropped)

    if storage is not None and bucket and max_ts >= cursor:
        save_feedback_cursor(storage, bucket, backend, max_ts)

    return {
        "backend": backend,
        "events_seen": len(events),
        "suppressions_written": suppressions_written,
        "ledger_dropped": ledger_dropped,
        "last_seen_ms": max_ts,
    }


__all__ = [
    "load_feedback_cursor",
    "save_feedback_cursor",
    "sync_feedback",
]
