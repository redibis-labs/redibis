"""Dedicated S3_METADATA_BUCKET keys stay isolated across workspaces."""

from __future__ import annotations

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.workspace.metadata_scope import contract_store_metadata_kwargs
from redibis.workspace.model import WorkspaceRef, _utc_now_iso
from redibis.workspace.prefix import PrefixedBackend
from redibis.workspace.promote import promote_table
from redibis.workspace.stores import WorkspaceStores


def _contract(table: str, name: str = "") -> dict:
    _, tbl = table.split(".", 1) if "." in table else ("data", table)
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": name or f"{tbl}_contract",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": tbl,
            "physicalName": table,
            "properties": [{"name": "id", "logicalType": "integer"}],
        }],
    }


def _ref(slug: str, root: str) -> WorkspaceRef:
    return WorkspaceRef(
        slug=slug, name=slug, kind="s3", root=root, created=_utc_now_iso(),
    )


def _store_for(ref: WorkspaceRef, backend, bucket: str) -> ContractStore:
    return ContractStore(
        backend, bucket=bucket,
        **contract_store_metadata_kwargs(ref, backend, bucket),
    )


def test_two_s3_workspaces_do_not_share_a_metadata_key_when_S3_METADATA_BUCKET_is_set(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("S3_METADATA_BUCKET", "meta-global")
    shared = LocalBackend(tmp_path / "minio")
    ref_a = _ref("team-a", "s3://contracts-a")
    ref_b = _ref("team-b", "s3://contracts-b")
    store_a = _store_for(ref_a, shared, "contracts-a")
    store_b = _store_for(ref_b, shared, "contracts-b")
    store_a.upsert(_contract("db.customers", name="a"), table="db.customers", workflow="manual")
    store_b.upsert(_contract("db.customers", name="b"), table="db.customers", workflow="manual")
    assert store_a.metadata.prefix != store_b.metadata.prefix
    assert store_a.metadata._provenance_key("db.customers") != store_b.metadata._provenance_key(
        "db.customers"
    )
    prov_a = store_a.get_metadata("db.customers")["provenance"]
    prov_b = store_b.get_metadata("db.customers")["provenance"]
    assert prov_a and prov_b
    assert prov_a != prov_b


def test_s3_workspaces_same_prefix_different_buckets_metadata_isolated(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("S3_METADATA_BUCKET", "meta-global")
    shared = LocalBackend(tmp_path / "minio")
    ref_a = _ref("ws-a", "s3://bucket-a/team-a")
    ref_b = _ref("ws-b", "s3://bucket-b/team-a")
    be_a = PrefixedBackend(shared, "team-a")
    be_b = PrefixedBackend(shared, "team-a")
    store_a = _store_for(ref_a, be_a, "bucket-a")
    store_b = _store_for(ref_b, be_b, "bucket-b")
    store_a.upsert(_contract("db.customers"), table="db.customers", workflow="manual")
    store_b.upsert(_contract("db.customers"), table="db.customers", workflow="manual")
    assert "bucket-a" in store_a.metadata.prefix
    assert "bucket-b" in store_b.metadata.prefix
    assert store_a.metadata.prefix != store_b.metadata.prefix
    assert store_a.metadata.backend is shared
    assert store_b.metadata.backend is shared


def test_default_workspace_metadata_layout_unchanged_with_S3_METADATA_BUCKET(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("S3_METADATA_BUCKET", "meta-global")
    store = ContractStore(LocalBackend(tmp_path / "def"), bucket="active-contracts")
    assert store.metadata.prefix == ""
    assert store.metadata.bucket == "meta-global"
    store.upsert(_contract("db.customers"), table="db.customers", workflow="manual")
    assert store.metadata._provenance_key("db.customers") == "db.customers/provenance.jsonl"


def test_s3_workspace_with_prefix_uses_namespaced_metadata_key(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("S3_METADATA_BUCKET", "meta-global")
    shared = LocalBackend(tmp_path / "minio")
    ref = _ref("telco", "s3://bkt/team-a")
    backend = PrefixedBackend(shared, "team-a")
    store = _store_for(ref, backend, "bkt")
    assert store.metadata.prefix == "workspaces/telco/bkt/team-a"
    assert store.metadata.backend is shared
    store.upsert(_contract("db.customers"), table="db.customers", workflow="manual")
    key = store.metadata._provenance_key("db.customers")
    assert key == "workspaces/telco/bkt/team-a/db.customers/provenance.jsonl"
    assert shared.exists("meta-global", key)


def test_open_workspace_root_applies_same_metadata_scope_as_kwargs(tmp_path, monkeypatch):
    monkeypatch.setenv("S3_METADATA_BUCKET", "meta-global")
    ref = _ref("cli-ws", "s3://contracts-a/pfx")
    backend = PrefixedBackend(LocalBackend(tmp_path / "minio"), "pfx")
    kw = contract_store_metadata_kwargs(ref, backend, "contracts-a")
    store = ContractStore(backend, bucket="contracts-a", **kw)
    assert store.metadata.prefix == "workspaces/cli-ws/contracts-a/pfx"
    local_kw = contract_store_metadata_kwargs(
        WorkspaceRef(
            slug="folder", name="folder", kind="local",
            root=str(tmp_path / "ws"), created=_utc_now_iso(),
        ),
        LocalBackend(tmp_path / "ws"),
        "",
    )
    assert local_kw == {}


def test_promote_copies_dedicated_metadata_when_S3_METADATA_BUCKET_is_set(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("S3_METADATA_BUCKET", "meta-global")
    shared = LocalBackend(tmp_path / "minio")
    src_ref = _ref("src", "s3://src-bkt/src-pfx")
    dst_ref = _ref("dst", "s3://dst-bkt/dst-pfx")
    src_store = _store_for(src_ref, PrefixedBackend(shared, "src-pfx"), "src-bkt")
    dst_store = _store_for(dst_ref, PrefixedBackend(shared, "dst-pfx"), "dst-bkt")
    table = "db.customers"
    src_store.upsert(_contract(table), table=table, workflow="manual")
    src = WorkspaceStores(src_ref, src_store)
    dst = WorkspaceStores(dst_ref, dst_store)
    from redibis.workspace import stores as wsmod
    real = wsmod.stores_for

    def _fake(slug):
        if slug == "dst":
            return dst
        if slug == "src":
            return src
        return real(slug)

    monkeypatch.setattr(wsmod, "stores_for", _fake)
    from redibis.workspace import promote as promo
    monkeypatch.setattr(promo, "stores_for", _fake)
    out = promote_table(src, table, target="dst", force=True, actor="ada")
    assert out["target"] == "dst"
    src_prov = src_store.get_metadata(table)["provenance"]
    dst_prov = dst_store.get_metadata(table)["provenance"]
    assert src_prov
    assert any(isinstance(p, dict) and p.get("workflow") == "manual" for p in dst_prov)
    assert any(isinstance(p, dict) and p.get("workflow") == "promote" for p in dst_prov)
    assert src_store.metadata.prefix != dst_store.metadata.prefix
