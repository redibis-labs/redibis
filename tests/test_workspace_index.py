"""Workspace index: cheap listing, search, paging, rebuild."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.workspace.index import WorkspaceIndex
from redibis.workspace.model import WorkspaceRef, _utc_now_iso
from redibis.workspace.prefix import PrefixedBackend
from redibis.workspace.stores import WorkspaceStores


def _contract(table: str, name: str = "") -> dict:
    _, tbl = table.split(".", 1) if "." in table else ("data", table)
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": name or f"{tbl}_contract",
        "version": "1.0.0",
        "status": "active",
        "contract_uuid": f"uuid-{table}",
        "schema": [{
            "name": tbl,
            "physicalName": table,
            "properties": [
                {"name": "id", "logicalType": "integer"},
                {"name": "email", "logicalType": "string", "tags": ["pii"]},
            ],
        }],
    }


class CountingBackend:
    """Delegates to LocalBackend/PrefixedBackend while counting body reads."""

    def __init__(self, inner):
        self._inner = inner
        self.gets: list[tuple[str, str]] = []

    def get_yaml(self, bucket, key):
        self.gets.append(("yaml", key))
        return self._inner.get_yaml(bucket, key)

    def get_json(self, bucket, key):
        self.gets.append(("json", key))
        return self._inner.get_json(bucket, key)

    def get_bytes(self, bucket, key):
        self.gets.append(("bytes", key))
        return self._inner.get_bytes(bucket, key)

    def get_text(self, bucket, key):
        self.gets.append(("text", key))
        return self._inner.get_text(bucket, key)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture(params=["local", "s3"])
def ws_pair(request, tmp_path):
    if request.param == "local":
        inner = LocalBackend(tmp_path / "ws")
        backend = inner
        bucket = ""
        prefix = ""
    else:
        inner = LocalBackend(tmp_path / "s3root")
        prefix = "ws/prefix"
        backend = PrefixedBackend(inner, prefix)
        bucket = "bkt"
    store = ContractStore(backend, bucket=bucket)
    ref = WorkspaceRef(
        slug="ix", name="ix", kind=request.param,
        root=str(tmp_path), created=_utc_now_iso(),
    )
    stores = WorkspaceStores(ref, store)
    return SimpleNamespace(kind=request.param, stores=stores, backend=backend, bucket=bucket)


def test_listing_reads_no_contract_bodies(ws_pair):
    for i in range(5):
        ws_pair.stores.contract.upsert(
            _contract(f"db.t{i}"), table=f"db.t{i}", workflow="manual",
        )
    counted = CountingBackend(ws_pair.backend)
    idx = WorkspaceIndex(counted, ws_pair.bucket, store=ws_pair.stores.contract)
    idx._rows = dict(ws_pair.stores.index._rows)
    idx._loaded = True
    page, total = idx.query(limit=3, offset=0)
    assert total == 5
    assert len(page) == 3
    body_reads = [g for g in counted.gets if g[0] in ("yaml", "bytes") and str(g[1]).startswith("active/")]
    assert body_reads == []


def test_search_matches_uuid_prefix_table_name_and_path(ws_pair):
    ws_pair.stores.contract.upsert(
        _contract("telecom.customers", name="customers_contract"),
        table="telecom.customers", workflow="manual",
    )
    ws_pair.stores.contract.upsert(
        _contract("telecom.billing", name="billing_contract"),
        table="telecom.billing", workflow="manual",
    )
    page, total = ws_pair.stores.index.query(q="CUSTOMERS")
    assert total == 1
    assert page[0].table == "telecom.customers"
    page, total = ws_pair.stores.index.query(q="uuid-telecom.bill")
    assert total == 1
    assert page[0].table == "telecom.billing"
    page, total = ws_pair.stores.index.query(q="active/telecom.customers")
    assert total == 1


def test_rebuild_matches_incremental_index(ws_pair):
    for i in range(4):
        ws_pair.stores.contract.upsert(
            _contract(f"db.t{i}"), table=f"db.t{i}", workflow="manual",
        )
    incremental = sorted((r.table, r.contract_uuid, r.columns) for r in ws_pair.stores.index.rows())
    ws_pair.stores.index.rebuild()
    rebuilt = sorted((r.table, r.contract_uuid, r.columns) for r in ws_pair.stores.index.rows())
    assert rebuilt == incremental
    assert len(rebuilt) == 4


def test_paging_is_stable_under_concurrent_upsert(ws_pair):
    for i in range(10):
        ws_pair.stores.contract.upsert(
            _contract(f"db.t{i:02d}"), table=f"db.t{i:02d}", workflow="manual",
        )
    errors: list[str] = []
    barrier = threading.Barrier(3)

    def reader():
        barrier.wait()
        for _ in range(20):
            page, total = ws_pair.stores.index.query(sort="table", desc=False, limit=5, offset=0)
            if len(page) != 5:
                errors.append(f"page {len(page)}")
            if total < 10:
                errors.append(f"total {total}")
            names = [r.table for r in page]
            if names != sorted(names):
                errors.append(f"unsorted {names}")

    def writer():
        barrier.wait()
        for i in range(10, 20):
            ws_pair.stores.contract.upsert(
                _contract(f"db.t{i:02d}"), table=f"db.t{i:02d}", workflow="manual",
            )

    threads = [threading.Thread(target=reader), threading.Thread(target=reader),
               threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    _, total = ws_pair.stores.index.query(limit=50, offset=0)
    assert total == 20


def test_index_hook_rebinds_after_invalidate(tmp_path):
    from redibis.workspace.stores import WorkspaceStores

    backend = LocalBackend(tmp_path / "ws")
    store = ContractStore(backend, bucket="")
    first = WorkspaceStores(
        WorkspaceRef(slug="a", name="a", kind="local", root=str(tmp_path), created=_utc_now_iso()),
        store,
    )
    store.upsert(_contract("db.one"), table="db.one", workflow="manual")
    assert any(r.table == "db.one" for r in first.index.rows())
    # Simulate cache rebuild against the same ContractStore.
    second = WorkspaceStores(
        WorkspaceRef(slug="a", name="a", kind="local", root=str(tmp_path), created=_utc_now_iso()),
        store,
    )
    store.upsert(_contract("db.two"), table="db.two", workflow="manual")
    assert any(r.table == "db.two" for r in second.index.rows())
