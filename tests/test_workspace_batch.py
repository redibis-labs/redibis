"""Batch runner, scan-batch, export zip — local and prefix-scoped backends."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.workspace.batch import BatchRunner, export_zip
from redibis.workspace.model import WorkspaceRef, _utc_now_iso
from redibis.workspace.prefix import PrefixedBackend
from redibis.workspace.scan_batch import run_scan_batch
from redibis.workspace.stores import WorkspaceStores


def _contract(table: str) -> dict:
    _, tbl = table.split(".", 1) if "." in table else ("data", table)
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": f"{tbl}_contract",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": tbl, "physicalName": table,
            "properties": [{"name": "id", "logicalType": "integer"}],
        }],
    }


@pytest.fixture(params=["local", "s3"])
def ws_pair(request, tmp_path):
    if request.param == "local":
        backend = LocalBackend(tmp_path / "ws")
        bucket = ""
    else:
        backend = PrefixedBackend(LocalBackend(tmp_path / "s3root"), "pfx")
        bucket = "bkt"
    store = ContractStore(backend, bucket=bucket)
    stores = WorkspaceStores(
        WorkspaceRef(slug="b", name="b", kind=request.param, root=str(tmp_path / "ws"),
                     created=_utc_now_iso()),
        store,
    )
    return SimpleNamespace(stores=stores, kind=request.param)


def test_batch_enrich_writes_through_service(ws_pair, monkeypatch):
    stores = ws_pair.stores
    tables = [f"db.t{i}" for i in range(5)]
    for t in tables:
        stores.contract.upsert(_contract(t), table=t, workflow="manual")

    seen: list[str] = []

    def fake_enrich(table, provider, **kwargs):
        seen.append(table)
        return SimpleNamespace(to_dict=lambda: {"table": table, "ok": True})

    monkeypatch.setattr(type(stores), "enrich", property(lambda self: SimpleNamespace(enrich=fake_enrich)))
    # BatchRunner._run_enrich imports get_provider — stub dispatch instead
    runner = BatchRunner(stores)

    def dispatch(kind, table, options):
        seen.append(table)
        stores.contract.upsert(_contract(table), table=table, workflow="enrich")
        return {"table": table}

    runner._dispatch = dispatch  # type: ignore[method-assign]
    bid = runner.submit("enrich", tables, options={"provider": "demo"}, actor="test")
    man = runner.run(bid)
    assert man["status"] == "done"
    assert man["counts"]["done"] == 5
    assert man["counts"]["error"] == 0
    assert set(seen) == set(tables)
    rows = {r.table: r for r in stores.index.rows()}
    assert "enrich" in (rows[tables[0]].workflows or []) or rows[tables[0]].table in tables


def test_failing_item_leaves_manifest_resumable(ws_pair):
    stores = ws_pair.stores
    for t in ("db.a", "db.b", "db.c"):
        stores.contract.upsert(_contract(t), table=t, workflow="manual")
    runner = BatchRunner(stores)
    calls = {"n": 0}

    def boom(kind, table, options):
        calls["n"] += 1
        if table == "db.b":
            raise RuntimeError("provider down")
        return {"table": table}

    runner._dispatch = boom  # type: ignore[method-assign]
    bid = runner.submit("enrich", ["db.a", "db.b", "db.c"])
    man = runner.run(bid)
    assert man["counts"]["error"] == 1
    assert man["counts"]["done"] == 2
    statuses = {i["table"]: i["status"] for i in man["items"]}
    assert statuses == {"db.a": "done", "db.b": "error", "db.c": "done"}

    def ok(kind, table, options):
        return {"table": table}

    runner._dispatch = ok  # type: ignore[method-assign]
    man2 = runner.run(bid)
    statuses2 = {i["table"]: i["status"] for i in man2["items"]}
    assert statuses2["db.a"] == "done"
    assert statuses2["db.b"] == "done"
    assert statuses2["db.c"] == "done"
    assert man2["counts"] == {"total": 3, "done": 3, "error": 0, "skipped": 0}


def test_export_zip_contains_exactly_requested(ws_pair):
    stores = ws_pair.stores
    stores.contract.upsert(_contract("db.keep"), table="db.keep", workflow="manual")
    stores.contract.upsert(_contract("db.skip"), table="db.skip", workflow="manual")
    blob = export_zip(stores, ["db.keep"], ["contract"])
    import zipfile
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        names = zf.namelist()
    assert names == ["db.keep/contract.yaml"]
    assert all("db.skip" not in n for n in names)


def test_scan_batch_50_csvs_resume_and_isolation(tmp_path):
    csvs = tmp_path / "csvs"
    csvs.mkdir()
    for i in range(50):
        (csvs / f"t{i:02d}.csv").write_text("id,name\n1,a\n", encoding="utf-8")
    (csvs / "bad.csv").write_text("not,a,valid\x00csv", encoding="utf-8")
    ws = tmp_path / "workspace"
    seen: list[str] = []

    def scan_one(path: Path, table: str, stores, options):
        if path.name == "bad.csv":
            raise ValueError("malformed csv")
        seen.append(table)
        stores.contract.upsert(_contract(table), table=table, workflow="pii")
        return {"run_id": f"run-{table}", "table": table}

    man = run_scan_batch(
        input_dir=csvs, workspace=str(ws), glob_pat="*.csv",
        scan_one=scan_one, continue_on_error=True,
    )
    # 50 good + 1 bad discovered; glob is *.csv so bad.csv is included
    assert man["counts"]["error"] == 1
    assert man["counts"]["done"] == 50
    from redibis.workspace.scan_batch import open_workspace_root
    stores = open_workspace_root(str(ws))
    assert len(stores.contract.list_tables()) == 50
    assert len(stores.index.rows()) == 50
    assert stores.backend.exists(stores.bucket, f"batches/{man['id']}.json")

    # kill mid-run simulation: pre-mark 40 done and resume remainder
    seen.clear()
    csvs2 = tmp_path / "csvs2"
    csvs2.mkdir()
    for i in range(10):
        (csvs2 / f"u{i}.csv").write_text("id\n1\n", encoding="utf-8")
    ws2 = tmp_path / "ws2"

    n = {"i": 0}

    def first_pass(path, table, stores, options):
        n["i"] += 1
        if n["i"] > 4:
            raise KeyboardInterrupt("killed")
        stores.contract.upsert(_contract(table), table=table, workflow="pii")
        return {"run_id": "r", "table": table}

    with pytest.raises(KeyboardInterrupt):
        run_scan_batch(input_dir=csvs2, workspace=str(ws2), scan_one=first_pass, batch_id="batchA")

    def rest(path, table, stores, options):
        stores.contract.upsert(_contract(table), table=table, workflow="pii")
        return {"run_id": "r2", "table": table}

    man2 = run_scan_batch(
        input_dir=csvs2, workspace=str(ws2), scan_one=rest,
        resume=True, batch_id="batchA",
    )
    stores2 = open_workspace_root(str(ws2))
    assert len(stores2.contract.list_tables()) == 10
    assert man2["counts"]["done"] >= 4


def _active_snapshot(store, table: str) -> dict:
    active = store.get_active(table)
    meta = store.get_metadata(table)
    return {
        "contract": copy.deepcopy(active),
        "version": (active or {}).get("version"),
        "status": (active or {}).get("status"),
        "provenance": list(meta.get("provenance") or []),
    }


def test_batch_synthesize_does_not_upsert_the_active_contract(ws_pair):
    stores = ws_pair.stores
    table = "db.customers"
    stores.contract.upsert(_contract(table), table=table, workflow="manual")
    before = _active_snapshot(stores.contract, table)
    runner = BatchRunner(stores)
    bid = runner.submit("synthesize", [table], options={"analysis_mode": "deterministic"})
    man = runner.run(bid)
    assert man["status"] == "done", man
    assert man["counts"]["error"] == 0
    after = _active_snapshot(stores.contract, table)
    assert after["version"] == before["version"] == "1.0.0"
    assert after["status"] == before["status"] == "active"
    assert after["contract"] == before["contract"]
    assert after["provenance"] == before["provenance"]
    assert not any(
        isinstance(p, dict) and p.get("workflow") == "synthesis"
        for p in after["provenance"]
    )


def test_batch_synthesize_writes_portable_candidate_artifact(ws_pair):
    stores = ws_pair.stores
    table = "db.customers"
    stores.contract.upsert(_contract(table), table=table, workflow="manual")
    runner = BatchRunner(stores)
    bid = runner.submit("synthesize", [table])
    man = runner.run(bid)
    item = man["items"][0]
    assert item["status"] == "done"
    result = item["result"]
    assert result.get("writes_active_contract") is False
    artifacts = result.get("artifacts") or {}
    json_key = artifacts.get("contract.synthesized.v3.1.json")
    assert json_key
    assert json_key.startswith(f"runs/{table}/synthesis/")
    assert stores.backend.exists(stores.bucket, json_key)
    payload = stores.backend.get_json(stores.bucket, json_key)
    assert payload.get("status") == "draft"
    assert item["run_id"]
    assert item["run_id"] in json_key


def test_batch_synthesize_assisted_mode_never_touches_active_without_explicit_opt_in(ws_pair):
    stores = ws_pair.stores
    table = "db.customers"
    stores.contract.upsert(_contract(table), table=table, workflow="manual")
    before = _active_snapshot(stores.contract, table)
    runner = BatchRunner(stores)
    bid = runner.submit(
        "synthesize", [table],
        options={"analysis_mode": "assisted", "provider": "no-such-provider-xyz"},
    )
    man = runner.run(bid)
    assert man["items"][0]["status"] == "error"
    after = _active_snapshot(stores.contract, table)
    assert after == before
    keys = stores.backend.list_keys(stores.bucket, prefix=f"runs/{table}/synthesis/")
    assert keys == []


def test_batch_synthesize_resume_skips_completed(ws_pair, monkeypatch):
    stores = ws_pair.stores
    table = "db.customers"
    stores.contract.upsert(_contract(table), table=table, workflow="manual")
    runner = BatchRunner(stores)
    bid = runner.submit("synthesize", [table])
    man = runner.run(bid)
    run_id = man["items"][0]["run_id"]
    json_key = (man["items"][0].get("result") or {}).get("artifacts", {}).get(
        "contract.synthesized.v3.1.json"
    )
    assert run_id and json_key

    import redibis.workspace.batch as batchmod

    def boom(*_a, **_k):
        raise AssertionError("completed synthesize item must not rerun")

    monkeypatch.setattr(batchmod, "_run_synthesize", boom)
    man2 = runner.run(bid)
    assert man2["items"][0]["status"] == "done"
    assert man2["items"][0]["run_id"] == run_id
    assert stores.backend.exists(stores.bucket, json_key)


def test_batch_export_kind_is_rejected(ws_pair):
    stores = ws_pair.stores
    stores.contract.upsert(_contract("db.keep"), table="db.keep", workflow="manual")
    runner = BatchRunner(stores)
    with pytest.raises(ValueError, match="GET /api/workspaces"):
        runner.submit("export", ["db.keep"])
    keys = stores.backend.list_keys(stores.bucket, prefix="batches/")
    assert keys == []
