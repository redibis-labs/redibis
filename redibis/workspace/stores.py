"""Every existing store, bound to one workspace's backend + prefix."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from redibis.store.contract_store import ContractStore
from redibis.store.review_store import ReviewStore
from redibis.store.storage_backend import StorageBackend
from redibis.workspace.backends import backend_for
from redibis.workspace.index import WorkspaceIndex, row_from_contract
from redibis.workspace.model import DEFAULT_SLUG, WorkspaceNotFound, WorkspaceRef
from redibis.workspace.registry import get_registry

log = logging.getLogger("redibis.workspace.stores")

_current_slug: ContextVar[str] = ContextVar("redibis_workspace", default=DEFAULT_SLUG)
_CACHE: dict[str, "WorkspaceStores"] = {}
_CACHE_LOCK = threading.Lock()


def current_slug() -> str:
    return _current_slug.get() or DEFAULT_SLUG


@contextmanager
def bind_workspace(slug: str) -> Iterator[str]:
    value = (slug or DEFAULT_SLUG).strip() or DEFAULT_SLUG
    token = _current_slug.set(value)
    try:
        yield value
    finally:
        _current_slug.reset(token)


def slug_from_request(request) -> str:
    """Path /api/workspaces/{slug}/… wins, then ?ws=, then X-Redibis-Workspace."""
    path = getattr(getattr(request, "url", None), "path", "") or ""
    if path.startswith("/api/workspaces/"):
        rest = path[len("/api/workspaces/"):]
        first = rest.split("/", 1)[0]
        if first and first not in {"",}:
            return first
    query = ""
    header = ""
    try:
        query = (request.query_params.get("ws") or "").strip()
    except Exception:
        query = ""
    try:
        header = (request.headers.get("X-Redibis-Workspace") or "").strip()
    except Exception:
        header = ""
    return query or header or DEFAULT_SLUG


class WorkspaceStores:
    """ContractStore / ReviewStore / index bound to one workspace."""

    def __init__(self, ref: WorkspaceRef, contract: ContractStore):
        self.ref = ref
        self.contract = contract
        # Tests sometimes monkeypatch a stub store with no backend. Keep
        # ``.contract`` usable so ``_cs()`` still returns the stub as-is.
        self.backend: StorageBackend | None = getattr(contract, "backend", None)
        self.bucket: str = getattr(contract, "bucket", "") or ""
        self._review = None
        self._index = None
        if self.backend is not None:
            self._attach_index_hook()

    @property
    def review(self):
        if self._review is None:
            if self.backend is None:
                raise AttributeError("workspace store has no backend")
            self._review = ReviewStore(self.backend, self.bucket)
        return self._review

    @property
    def index(self):
        if self._index is None:
            if self.backend is None:
                raise AttributeError("workspace store has no backend")
            self._index = WorkspaceIndex(self.backend, self.bucket, store=self.contract)
        return self._index

    @property
    def profiles(self):
        return self.contract.profiles

    @property
    def ledger(self):
        return self.contract.generation_ledger

    @property
    def enrich(self):
        from redibis.enrich.service import enrichment_service_for_store
        return enrichment_service_for_store(self.contract)

    @property
    def steward(self):
        from redibis.services.steward_review_service import StewardReviewService
        return StewardReviewService(self.contract)

    def _attach_index_hook(self) -> None:
        def hook(result, slim, *, _self=self) -> None:
            try:
                row = row_from_contract(
                    _self.contract, result.table, slim, review=_self.review,
                )
                _self.index.upsert(row)
            except Exception:
                log.warning("workspace index upsert failed for %s", result.table, exc_info=True)

        # Re-bind on every WorkspaceStores construction so invalidate_stores()
        # does not leave upserts writing into an orphaned in-memory index.
        prev = getattr(self.contract, "on_upsert", None)
        old_ws = getattr(self.contract, "_ws_index_hook", None)
        base = getattr(self.contract, "_ws_index_base", None)
        if base is None and callable(prev) and prev is not old_ws:
            base = prev
            self.contract._ws_index_base = base
        if callable(base):
            def chained(result, slim, _prev=base, _hook=hook) -> None:
                try:
                    _prev(result, slim)
                finally:
                    _hook(result, slim)
            self.contract.on_upsert = chained
        else:
            self.contract.on_upsert = hook
        self.contract._ws_index_hook = hook

    def touch(self, table: str):
        return self.index.touch(table)


def _default_contract_store() -> ContractStore:
    """Prefer the webapp-bound getter so tests that monkeypatch it still work."""
    try:
        from redibis.webapp import backend as web
        fn = getattr(web, "get_contract_store", None)
        if callable(fn):
            return fn()
    except Exception:
        pass
    from redibis.webapp.store_accessors import get_contract_store
    return get_contract_store()


def _build(slug: str) -> WorkspaceStores:
    registry = get_registry()
    if slug == DEFAULT_SLUG:
        contract = _default_contract_store()
        return WorkspaceStores(registry.default(), contract)
    ref = registry.get(slug)
    backend, bucket = backend_for(ref)
    contract = ContractStore(backend, bucket=bucket)
    return WorkspaceStores(ref, contract)


def stores_for(slug: str) -> WorkspaceStores:
    slug = (slug or DEFAULT_SLUG).strip() or DEFAULT_SLUG
    with _CACHE_LOCK:
        cached = _CACHE.get(slug)
        if slug == DEFAULT_SLUG:
            contract = _default_contract_store()
            if cached is not None and cached.contract is contract:
                return cached
            built = WorkspaceStores(get_registry().default(), contract)
            _CACHE[slug] = built
            return built
        if cached is not None:
            return cached
        built = _build(slug)
        _CACHE[slug] = built
        return built


def current_stores(request=None) -> WorkspaceStores:
    slug = slug_from_request(request) if request is not None else current_slug()
    return stores_for(slug)


def invalidate_stores(slug: Optional[str] = None) -> None:
    with _CACHE_LOCK:
        if slug is None:
            _CACHE.clear()
        else:
            _CACHE.pop(slug, None)
