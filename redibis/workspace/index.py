"""Cheap listing index for a workspace. Paging/search never read contract bodies."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from redibis.contracts.privacy import column_is_pii
from redibis.review.artifacts import list_artifacts as list_steward_artifacts
from redibis.store.contract_store import ContractStore
from redibis.store.review_store import ReviewState, ReviewStore
from redibis.store.storage_backend import StorageBackend
from redibis.workspace.atomic import atomic_put_json
from redibis.workspace.model import WORKSPACE_LAYOUT

log = logging.getLogger("redibis.workspace.index")

INDEX_KEY = WORKSPACE_LAYOUT["index"]
SORT_KEYS = ("updated", "table", "columns", "pii_columns", "review")
ARTIFACT_ORDER = ("contract", "verdicts", "evidence", "llm_context", "corpus", "graph")


@dataclass(frozen=True)
class IndexRow:
    contract_uuid: str
    table: str
    name: str
    path: str
    version: str
    status: str
    columns: int = 0
    pii_columns: int = 0
    review: str = "none"  # none | in_progress | guaranteed
    artifacts: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    updated: str = ""
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IndexRow":
        arts = raw.get("artifacts") or []
        if not isinstance(arts, list):
            arts = list(arts)
        wfs = raw.get("workflows") or []
        if not isinstance(wfs, list):
            wfs = list(wfs)
        return cls(
            contract_uuid=str(raw.get("contract_uuid") or ""),
            table=str(raw.get("table") or ""),
            name=str(raw.get("name") or ""),
            path=str(raw.get("path") or ""),
            version=str(raw.get("version") or ""),
            status=str(raw.get("status") or ""),
            columns=int(raw.get("columns") or 0),
            pii_columns=int(raw.get("pii_columns") or 0),
            review=str(raw.get("review") or "none"),
            artifacts=[str(a) for a in arts],
            workflows=[str(w) for w in wfs if w],
            updated=str(raw.get("updated") or ""),
            size=int(raw.get("size") or 0),
        )


def _iter_props(contract: dict) -> list[dict]:
    out: list[dict] = []
    for schema in contract.get("schema") or []:
        if not isinstance(schema, dict):
            continue
        for prop in schema.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name"):
                out.append(prop)
    return out


def _review_chip(state: ReviewState) -> str:
    if state.guaranteed:
        return "guaranteed"
    if state.updated_at or state.columns:
        return "in_progress"
    return "none"


def _artifact_names(store: ContractStore, table: str) -> list[str]:
    try:
        listing = list_steward_artifacts(store, table)
    except Exception:
        return []
    arts = listing.get("artifacts") or {}
    present = []
    for name in ARTIFACT_ORDER:
        if arts.get(name):
            present.append(name)
    return present


def row_from_contract(
    store: ContractStore,
    table: str,
    slim: dict,
    *,
    review: ReviewStore | None = None,
    size: int = 0,
) -> IndexRow:
    props = _iter_props(slim)
    meta = {}
    try:
        meta = store.get_metadata(table) or {}
    except Exception:
        meta = {}
    workflows = sorted({
        p.get("workflow") for p in (meta.get("provenance") or [])
        if isinstance(p, dict) and p.get("workflow")
    })
    updated = (meta.get("telemetry") or {}).get("last_updated") or ""
    rs = review.get(table) if review is not None else ReviewState(table=table)
    return IndexRow(
        contract_uuid=str(slim.get("contract_uuid") or ""),
        table=table,
        name=str(slim.get("name") or table),
        path=store._active_key(table),
        version=str(slim.get("version") or ""),
        status=str(slim.get("status") or ""),
        columns=len(props),
        pii_columns=sum(1 for p in props if column_is_pii(p)),
        review=_review_chip(rs),
        artifacts=_artifact_names(store, table),
        workflows=[str(w) for w in workflows],
        updated=str(updated),
        size=size,
    )


class WorkspaceIndex:
    """In-memory listing over ``index.json``. ``query`` does not touch storage."""

    def __init__(self, backend: StorageBackend, bucket: str, store: ContractStore | None = None):
        self.backend = backend
        self.bucket = bucket
        self.store = store
        self._lock = threading.RLock()
        self._rows: dict[str, IndexRow] = {}
        self._loaded = False

    def _persist(self) -> None:
        payload = {"rows": [r.to_dict() for r in self._rows.values()]}
        atomic_put_json(self.backend, self.bucket, INDEX_KEY, payload)

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._rows = {}
            if self.backend.exists(self.bucket, INDEX_KEY):
                try:
                    raw = self.backend.get_json(self.bucket, INDEX_KEY) or {}
                    for row in raw.get("rows") or []:
                        if isinstance(row, dict) and row.get("table"):
                            item = IndexRow.from_dict(row)
                            self._rows[item.table] = item
                except Exception:
                    log.warning("workspace index unreadable — will rebuild on demand", exc_info=True)
            self._loaded = True

    def rows(self) -> list[IndexRow]:
        self.load()
        with self._lock:
            return list(self._rows.values())

    def upsert(self, row: IndexRow) -> None:
        self.load()
        with self._lock:
            self._rows[row.table] = row
            self._persist()

    def remove(self, table: str) -> None:
        self.load()
        with self._lock:
            self._rows.pop(table, None)
            self._persist()

    def touch(self, table: str) -> Optional[IndexRow]:
        """Re-read one contract + overlays and upsert the row."""
        store = self.store
        if store is None:
            return None
        slim = store.get_active(table)
        if slim is None:
            self.remove(table)
            return None
        review = ReviewStore(store.backend, store.bucket)
        row = row_from_contract(store, table, slim, review=review)
        self.upsert(row)
        return row

    def rebuild(self) -> int:
        store = self.store
        if store is None:
            raise RuntimeError("WorkspaceIndex.rebuild requires a ContractStore")
        review = ReviewStore(store.backend, store.bucket)
        built: dict[str, IndexRow] = {}
        for table in store.list_tables():
            slim = store.get_active(table)
            if not slim:
                continue
            built[table] = row_from_contract(store, table, slim, review=review)
        with self._lock:
            self._rows = built
            self._loaded = True
            self._persist()
        return len(built)

    def query(
        self,
        *,
        q: str = "",
        status: str = "",
        review: str = "",
        sort: str = "updated",
        desc: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[IndexRow], int]:
        self.load()
        needle = (q or "").strip().lower()
        status_f = (status or "").strip().lower()
        review_f = (review or "").strip().lower()
        sort_key = sort if sort in SORT_KEYS else "updated"
        with self._lock:
            items = list(self._rows.values())
        matched: list[IndexRow] = []
        for row in items:
            if status_f and (row.status or "").lower() != status_f:
                continue
            if review_f and (row.review or "").lower() != review_f:
                continue
            if needle:
                uuid = (row.contract_uuid or "").lower()
                hay = (
                    (row.table or "").lower(),
                    (row.name or "").lower(),
                    (row.path or "").lower(),
                )
                if not (any(needle in h for h in hay) or uuid.startswith(needle)):
                    continue
            matched.append(row)

        def _sort_val(row: IndexRow):
            val = getattr(row, sort_key)
            if val is None:
                return ""
            return val

        matched.sort(key=_sort_val, reverse=bool(desc))
        total = len(matched)
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        page = matched[offset: offset + limit] if limit else matched[offset:]
        return page, total
