"""HTTP surface: bounded /api/contracts, workspace CRUD, batches, promote."""

from __future__ import annotations

import pytest

from redibis.store.contract_store import ContractStore
from redibis.store.review_store import ColumnReview, ReviewStore
from redibis.store.storage_backend import LocalBackend
from redibis.workspace.stores import invalidate_stores

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


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


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(root))
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("REDIBIS_AUTH_ENABLED", "0")
    from redibis.webapp.store_accessors import clear_stores
    clear_stores()
    invalidate_stores()
    store = ContractStore(LocalBackend(str(root)), bucket="active-contracts")
    from redibis.webapp import backend as web
    monkeypatch.setattr(web, "get_contract_store", lambda: store)
    invalidate_stores()
    client = TestClient(web.app)
    return client, store


def test_api_contracts_is_bounded_by_default(api_env):
    client, store = api_env
    for i in range(60):
        store.upsert(_contract(f"db.t{i:03d}"), table=f"db.t{i:03d}", workflow="manual")
    invalidate_stores()
    r = client.get("/api/contracts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 50
    assert r.headers.get("X-Redibis-Total") == "60"
    assert r.headers.get("X-Redibis-Truncated") == "1"
    r2 = client.get("/api/contracts?all=1")
    assert len(r2.json()) == 60


def test_workspace_list_includes_default(api_env):
    client, _store = api_env
    r = client.get("/api/workspaces")
    assert r.status_code == 200
    slugs = [w["slug"] for w in r.json()["workspaces"]]
    assert "default" in slugs


def test_unknown_workspace_is_404(api_env):
    client, _store = api_env
    r = client.get("/api/workspaces/no-such-ws/contracts")
    assert r.status_code == 404


def test_promote_refuses_pending_and_copies_guaranteed(api_env, tmp_path, monkeypatch):
    client, store = api_env
    table = "db.customers"
    store.upsert(_contract(table), table=table, workflow="manual")
    r = client.post(f"/api/workspaces/default/contracts/{table}/promote", json={"target": "default"})
    # default→default of a pending contract is refused
    assert r.status_code in (403, 400)

    reviews = ReviewStore(store.backend, store.bucket)
    for col in ("id", "email"):
        reviews.set_column(table, ColumnReview(column=col, status="approved", reviewed_by="ada"),
                           total_columns=2, required_table_items=[])
    # table-level items still pending — force the flag for this unit test
    state = reviews.get(table)
    state.guaranteed = True
    reviews._save(table, state)

    # Promote into a second local store by monkeypatching stores_for target
    other = ContractStore(LocalBackend(tmp_path / "other"), bucket="active-contracts")

    from redibis.workspace import stores as wsmod
    from redibis.workspace.model import WorkspaceRef, _utc_now_iso
    from redibis.workspace.stores import WorkspaceStores

    real = wsmod.stores_for

    def _fake(slug):
        if slug == "other":
            return WorkspaceStores(
                WorkspaceRef(slug="other", name="other", kind="local",
                             root=str(tmp_path / "other"), created=_utc_now_iso()),
                other,
            )
        return real(slug)

    monkeypatch.setattr(wsmod, "stores_for", _fake)
    from redibis.workspace import promote as promo
    monkeypatch.setattr(promo, "stores_for", _fake)

    from redibis.workspace.promote import promote_table
    src = real("default")
    out = promote_table(src, table, target="other", force=False, actor="ada")
    assert out["target"] == "other"
    assert other.get_active(table) is not None
    dest_meta = other.get_metadata(table)
    prov = dest_meta.get("provenance") or []
    promo = [p for p in prov if isinstance(p, dict) and p.get("workflow") == "promote"]
    assert promo, dest_meta
    assert promo[-1].get("source_workspace") == "default"
    assert promo[-1].get("forced") is False
    assert promo[-1].get("actor") == "ada"
    assert store.get_active(table) is not None


def test_promote_force_records_provenance(api_env, tmp_path, monkeypatch):
    client, store = api_env
    table = "db.pending"
    store.upsert(_contract(table), table=table, workflow="manual")
    other = ContractStore(LocalBackend(tmp_path / "forced"), bucket="active-contracts")
    from redibis.workspace import stores as wsmod
    from redibis.workspace.model import WorkspaceRef, _utc_now_iso
    from redibis.workspace.stores import WorkspaceStores
    from redibis.workspace import promote as promo

    real = wsmod.stores_for

    def _fake(slug):
        if slug == "forced":
            return WorkspaceStores(
                WorkspaceRef(slug="forced", name="forced", kind="local",
                             root=str(tmp_path / "forced"), created=_utc_now_iso()),
                other,
            )
        return real(slug)

    monkeypatch.setattr(wsmod, "stores_for", _fake)
    monkeypatch.setattr(promo, "stores_for", _fake)
    out = promo.promote_table(real("default"), table, target="forced", force=True, actor="root")
    assert out["forced"] is True
    prov = other.get_metadata(table).get("provenance") or []
    promo_entries = [p for p in prov if isinstance(p, dict) and p.get("workflow") == "promote"]
    assert promo_entries[-1].get("forced") is True


def test_workspace_contracts_page_and_export(api_env):
    client, store = api_env
    for i in range(3):
        store.upsert(_contract(f"db.p{i}", name=f"page_{i}"), table=f"db.p{i}", workflow="manual")
    invalidate_stores()
    r = client.get("/api/workspaces/default/contracts?limit=2&offset=0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["rows"]) == 2
    assert {"contract_uuid", "table", "name", "path"} <= set(body["rows"][0])
    z = client.get("/api/workspaces/default/export?tables=db.p0&artifacts=contract")
    assert z.status_code == 200
    assert z.headers["content-type"].startswith("application/zip")
    assert "db.p0" in z.headers.get("content-disposition", "") or z.content[:2] == b"PK"


def test_add_local_workspace_respects_allowed_roots(api_env, tmp_path, monkeypatch):
    client, _store = api_env
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    folder = allowed / "telco"
    folder.mkdir()
    import yaml
    cfg = tmp_path / "redibis.yaml"
    cfg.write_text(yaml.safe_dump({"workspaces": {"allowed_roots": [str(allowed)]}}), encoding="utf-8")
    monkeypatch.setenv("REDIBIS_CONFIG", str(cfg))
    from redibis.workspace.registry import reset_registry
    reset_registry()
    invalidate_stores()
    denied = client.post("/api/workspaces", json={"name": "nope", "kind": "local", "root": str(tmp_path / "elsewhere")})
    assert denied.status_code in (400, 403)
    ok = client.post("/api/workspaces", json={"name": "telco", "kind": "local", "root": str(folder)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["slug"]
    slugs = [w["slug"] for w in client.get("/api/workspaces").json()["workspaces"]]
    assert ok.json()["slug"] in slugs
    gone = client.delete(f"/api/workspaces/{ok.json()['slug']}")
    assert gone.status_code == 200
    assert folder.exists()
    assert gone.json().get("data_deleted") is False
