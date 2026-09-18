"""Contract workspaces — registry, containment, prefixed backend, routing seam."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.workspace.backends import backend_for
from redibis.workspace.index import WorkspaceIndex
from redibis.workspace.model import WorkspaceDenied, WorkspaceNotFound, WorkspaceRef, _utc_now_iso
from redibis.workspace.paths import resolve_local_folder
from redibis.workspace.prefix import PrefixedBackend
from redibis.workspace.registry import WorkspaceRegistry, reset_registry
from redibis.workspace.stores import WorkspaceStores, invalidate_stores, stores_for


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
            "properties": [
                {"name": "id", "logicalType": "integer"},
                {"name": "email", "logicalType": "string", "tags": ["pii"]},
            ],
        }],
    }


@pytest.fixture(params=["local", "s3"])
def ws_pair(request, tmp_path):
    """Same tests against a local folder backend and a prefix-scoped (MinIO-like) one."""
    if request.param == "local":
        backend = LocalBackend(tmp_path / "ws")
        bucket = ""
        inner = backend
        prefix = ""
    else:
        inner = LocalBackend(tmp_path / "s3root")
        prefix = "reviews/q3"
        backend = PrefixedBackend(inner, prefix)
        bucket = "contracts"
    store = ContractStore(backend, bucket=bucket)
    ref = WorkspaceRef(
        slug="telco", name="telco", kind=request.param,
        root=str(tmp_path / "ws") if request.param == "local" else f"s3://{bucket}/{prefix}",
        created=_utc_now_iso(),
    )
    stores = WorkspaceStores(ref, store)
    return SimpleNamespace(
        kind=request.param, stores=stores, inner=inner,
        prefix=prefix, bucket=bucket, backend=backend,
    )


@pytest.fixture()
def registry_env(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    configs = tmp_path / "configs"
    configs.mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    cfg = tmp_path / "redibis.yaml"
    cfg.write_text(yaml.safe_dump({"workspaces": {"allowed_roots": [str(allowed)]}}), encoding="utf-8")
    monkeypatch.setenv("REDIBIS_CONFIG", str(cfg))
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    reset_registry()
    invalidate_stores()
    yield SimpleNamespace(
        allowed=allowed, configs=configs, storage=storage,
        registry=WorkspaceRegistry(configs / "workspaces.json"),
    )
    reset_registry()
    invalidate_stores()


def test_default_workspace_is_the_global_store(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    from redibis.webapp.store_accessors import clear_stores, get_contract_store
    clear_stores()
    invalidate_stores()
    global_store = get_contract_store()
    ws = stores_for("default")
    assert ws.contract is global_store
    assert ws.contract.backend is global_store.backend
    assert ws.contract.bucket == global_store.bucket


def test_local_workspace_outside_allowed_roots_is_refused(registry_env, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(WorkspaceDenied, match="outside"):
        registry_env.registry.add_local("nope", str(outside))


def test_local_workspace_inside_configs_dir_is_refused(registry_env):
    sneak = registry_env.configs / "inside"
    sneak.mkdir()
    # Even if we add configs as an allowed root, the dedicated check fires.
    with pytest.raises(WorkspaceDenied, match="configs"):
        resolve_local_folder(
            sneak, [registry_env.configs],
            configs=registry_env.configs,
            storage_root=registry_env.storage,
        )


def test_symlinked_folder_escaping_allowed_root_is_refused(registry_env, tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    link = registry_env.allowed / "escape"
    try:
        link.symlink_to(secret, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(WorkspaceDenied, match="outside"):
        registry_env.registry.add_local("evil", str(link))


def test_s3_workspace_scopes_every_key_under_prefix(ws_pair):
    if ws_pair.kind != "s3":
        pytest.skip("prefix-scoped backend only")
    ws_pair.stores.contract.upsert(_contract("db.customers"), table="db.customers", workflow="manual")
    keys = ws_pair.inner.list_keys(ws_pair.bucket)
    assert keys
    assert all(k == ws_pair.prefix or k.startswith(ws_pair.prefix + "/") for k in keys)


def test_removing_a_workspace_never_deletes_its_data(registry_env):
    folder = registry_env.allowed / "telco"
    folder.mkdir()
    marker = folder / "keep-me.txt"
    marker.write_text("hello", encoding="utf-8")
    ref = registry_env.registry.add_local("telco", str(folder))
    registry_env.registry.remove(ref.slug)
    with pytest.raises(WorkspaceNotFound):
        registry_env.registry.get(ref.slug)
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "hello"


def test_empty_allowed_roots_disables_local_workspaces(tmp_path):
    with pytest.raises(WorkspaceDenied, match="disabled"):
        resolve_local_folder(tmp_path, [], must_exist=False)


def test_no_contract_route_calls_the_global_accessor_directly():
    root = Path(__file__).resolve().parents[1]
    backend = (root / "redibis" / "webapp" / "backend.py").read_text(encoding="utf-8")
    tree = ast.parse(backend)

    def _route_path(node: ast.AST) -> str:
        for dec in getattr(node, "decorator_list", []) or []:
            if isinstance(dec, ast.Call) and dec.args:
                arg0 = dec.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    return arg0.value
        return ""

    offenders = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        path = _route_path(node)
        if "/api/contracts" not in path and "/api/synthesis" not in path:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id == "get_contract_store":
                offenders.append(f"{node.name}:{path}")
            if isinstance(child, ast.Attribute) and child.attr == "get_contract_store":
                offenders.append(f"{node.name}:{path}")
    assert offenders == []

    for rel in (
        "redibis/webapp/review_routes.py",
        "redibis/webapp/steward_routes.py",
        "redibis/webapp/workspace_routes.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert "get_contract_store(" not in text
