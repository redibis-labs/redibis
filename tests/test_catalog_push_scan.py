"""Tests for scan-folder catalog push, filters, and OM version routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from redibis.config import CatalogConfig
from redibis.services.catalog.openmetadata import (
    build_native_data_contract_payload,
    parse_om_version,
    uses_odcs_data_contract_api,
)
from redibis.services.catalog.push_run import CatalogPushRunStore
from redibis.services.catalog.scan_tables import filter_tables, tables_from_scan_dir
from redibis.services.catalog_service import CatalogService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
TABLE = "telecom.customers"


@pytest.fixture
def contract() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def test_tables_from_scan_dir(tmp_path):
    s1 = tmp_path / "sess1"
    s1.mkdir()
    (s1 / "session.json").write_text(
        json.dumps({"session_id": "sess1", "table_name": "telecom.customers"}),
        encoding="utf-8",
    )
    s2 = tmp_path / "sess2"
    s2.mkdir()
    (s2 / "session.json").write_text(
        json.dumps({"session_id": "sess2", "table_name": "telecom.orders"}),
        encoding="utf-8",
    )
    # duplicate table
    s3 = tmp_path / "sess3"
    s3.mkdir()
    (s3 / "session.json").write_text(
        json.dumps({"session_id": "sess3", "table_name": "telecom.customers"}),
        encoding="utf-8",
    )
    tables = tables_from_scan_dir(tmp_path)
    assert tables == ["telecom.customers", "telecom.orders"]


def test_filter_tables_database_include_exclude(tmp_path):
    candidates = [
        "telecom.customers",
        "telecom.orders",
        "retail.orders",
        "telecom.sample",
    ]
    assert filter_tables(candidates, database="telecom") == [
        "telecom.customers", "telecom.orders", "telecom.sample",
    ]
    assert filter_tables(candidates, include=["telecom.*"], exclude=["*.sample"]) == [
        "telecom.customers", "telecom.orders",
    ]
    tf = tmp_path / "tables.txt"
    tf.write_text("telecom.customers\n# comment\nretail.orders\n", encoding="utf-8")
    assert filter_tables(candidates, tables_file=tf) == [
        "telecom.customers", "retail.orders",
    ]


def test_parse_om_version_and_strategy():
    assert parse_om_version("1.11.13") == (1, 11, 13)
    assert parse_om_version({"version": "1.12.0"}) == (1, 12, 0)
    assert uses_odcs_data_contract_api((1, 11, 13)) is False
    assert uses_odcs_data_contract_api((1, 12, 0)) is True


def test_build_native_data_contract_payload(contract):
    payload = build_native_data_contract_payload(
        contract, TABLE, table_id="abc-123", table_fqn="redibis.telecom.default.customers",
    )
    assert "status" not in payload
    assert payload["entityStatus"] == "Active"
    assert payload["entity"]["id"] == "abc-123"
    assert payload["entity"]["type"] == "table"
    assert any(c["name"] == "msisdn" for c in payload["schema"])


def test_push_scan_dry_run_manifest(contract, tmp_path):
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    store.upsert(contract, TABLE, workflow="manual", run_id="seed")

    scan = tmp_path / "scan"
    sess = scan / "s1"
    sess.mkdir(parents=True)
    (sess / "session.json").write_text(
        json.dumps({"session_id": "s1", "table_name": TABLE}),
        encoding="utf-8",
    )

    svc = CatalogService(store=store, config=CatalogConfig())
    report = svc.push_scan(scan, dry_run=True)
    assert report["summary"]["succeeded"] == 1
    assert report["summary"]["failed"] == 0
    assert Path(report["manifest"]).is_file()
    manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "succeeded"
    assert manifest["tasks"][0]["table"] == TABLE


def test_skip_in_sync_check_failure_still_pushes_but_is_recorded(contract, tmp_path, monkeypatch):
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    store.upsert(contract, TABLE, workflow="manual", run_id="seed")

    scan = tmp_path / "scan"
    sess = scan / "s1"
    sess.mkdir(parents=True)
    (sess / "session.json").write_text(
        json.dumps({"session_id": "s1", "table_name": TABLE}),
        encoding="utf-8",
    )

    svc = CatalogService(store=store, config=CatalogConfig())

    def _boom_status(table, **kwargs):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(svc, "status", _boom_status)
    report = svc.push_scan(scan, dry_run=True, skip_in_sync=True)
    assert report["summary"]["succeeded"] == 1
    assert report["summary"]["excluded"] == 0
    failures = report["run"]["filters"]["skip_in_sync_check_failures"]
    assert TABLE in failures
    assert "catalog unreachable" in failures[TABLE]


def test_push_scan_continues_after_failure(contract, tmp_path, monkeypatch):
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    store.upsert(contract, TABLE, workflow="manual", run_id="seed")
    other = yaml.safe_load(yaml.dump(contract))
    other["physicalName"] = "telecom.orders"
    other["table_name"] = "orders"
    other["schema"][0]["physicalName"] = "telecom.orders"
    other["schema"][0]["name"] = "telecom_orders"
    store.upsert(other, "telecom.orders", workflow="manual", run_id="seed2")

    scan = tmp_path / "scan"
    for i, table in enumerate([TABLE, "telecom.orders"]):
        d = scan / f"s{i}"
        d.mkdir(parents=True)
        (d / "session.json").write_text(
            json.dumps({"session_id": f"s{i}", "table_name": table}),
            encoding="utf-8",
        )

    svc = CatalogService(store=store, config=CatalogConfig())
    calls = {"n": 0}

    def _boom(table, **kwargs):
        calls["n"] += 1
        if table == TABLE:
            raise RuntimeError("simulated push failure")
        from redibis.services.catalog.base import CatalogPushResult
        return CatalogPushResult(
            table=table, backend="openmetadata", entity_fqn=f"x.{table}", dry_run=True,
        )

    monkeypatch.setattr(svc, "push", _boom)
    report = svc.push_scan(scan, dry_run=True)
    assert report["summary"]["failed"] == 1
    assert report["summary"]["succeeded"] == 1
    assert calls["n"] == 2


def test_push_run_resume_skips_succeeded(tmp_path):
    store = CatalogPushRunStore(tmp_path)
    run = store.create(
        ["telecom.customers", "telecom.orders"],
        backend="openmetadata",
        versions={"telecom.customers": "1.0.0", "telecom.orders": "1.0.0"},
    )
    task = run.task_map()["telecom.customers"]
    task.status = "succeeded"
    task.contract_version = "1.0.0"
    store.save(run)
    todo = store.resolve_resume(
        run,
        active_versions={"telecom.customers": "1.0.0", "telecom.orders": "1.0.0"},
    )
    assert todo == ["telecom.orders"]


def test_push_run_resume_version_change_with_empty_stored_version(tmp_path):
    store = CatalogPushRunStore(tmp_path)
    run = store.create(
        ["telecom.customers"],
        backend="openmetadata",
        versions={"telecom.customers": ""},
    )
    task = run.task_map()["telecom.customers"]
    task.status = "succeeded"
    task.contract_version = ""
    store.save(run)
    todo = store.resolve_resume(
        run, active_versions={"telecom.customers": "2.0.0"},
    )
    assert todo == ["telecom.customers"]
    assert task.status == "pending"


def test_native_payload_uses_matching_schema_not_first():
    contract = {
        "name": "multi",
        "status": "active",
        "schema": [
            {
                "name": "other",
                "physicalName": "telecom.other",
                "properties": [{"name": "wrong_col", "logicalType": "string"}],
            },
            {
                "name": "customers",
                "physicalName": "telecom.customers",
                "properties": [{"name": "msisdn", "logicalType": "string"}],
            },
        ],
    }
    payload = build_native_data_contract_payload(
        contract, "telecom.customers", table_id="abc", table_fqn="x.telecom.default.customers",
    )
    names = [c["name"] for c in payload["schema"]]
    assert names == ["msisdn"]
    assert "wrong_col" not in names


def test_find_data_contract_for_entity_paginates(monkeypatch):
    import httpx

    from redibis.services.catalog.openmetadata import OpenMetadataSettings, _OpenMetadataClient

    client = _OpenMetadataClient(
        OpenMetadataSettings(
            host="http://om.local", jwt="t", service_name="redibis", default_schema="default",
        )
    )

    class FakeResp:
        status_code = 404
        content = b""

        def json(self):
            return {}

    class FakeHttpxClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeHttpxClient)

    pages = [
        {"data": [{"id": "c1", "entity": {"id": "other"}}], "paging": {"after": "p2"}},
        {"data": [{"id": "c2", "entity": {"id": "abc-123"}}], "paging": {}},
    ]
    calls = {"n": 0}

    def fake_request(method, path, *, json_body=None, query=None):
        calls["n"] += 1
        return pages[calls["n"] - 1]

    monkeypatch.setattr(client, "_request", fake_request)

    found = client.find_data_contract_for_entity("abc-123")
    assert found is not None
    assert found["id"] == "c2"
    assert calls["n"] == 2


def test_uses_odcs_rejects_unparseable_version():
    with pytest.raises(ValueError, match="parse OpenMetadata"):
        uses_odcs_data_contract_api((0, 0, 0))


def test_push_scan_resume_rebinding_and_rejects_dry_run(contract, tmp_path, monkeypatch):
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    store.upsert(contract, TABLE, workflow="manual", run_id="seed")

    scan = tmp_path / "scan"
    sess = scan / "s1"
    sess.mkdir(parents=True)
    (sess / "session.json").write_text(
        json.dumps({"session_id": "s1", "table_name": TABLE}),
        encoding="utf-8",
    )

    svc = CatalogService(store=store, config=CatalogConfig())

    def _fail(table, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "push", _fail)
    report = svc.push_scan(scan, dry_run=False)
    run_id = report["run_id"]
    manifest = Path(report["manifest"])
    assert report["summary"]["failed"] == 1

    # Dry-run resume of a real failed run must be rejected.
    with pytest.raises(ValueError, match="Refusing --dry-run"):
        svc.push_scan(tmp_path / "other_scan", resume=str(manifest), dry_run=True)

    calls = {"n": 0}

    def _ok(table, **kwargs):
        calls["n"] += 1
        from redibis.services.catalog.base import CatalogPushResult
        return CatalogPushResult(
            table=table, backend="openmetadata", entity_fqn=f"x.{table}",
            details={"om_version": "1.11.13", "contract_strategy": "native"},
        )

    monkeypatch.setattr(svc, "push", _ok)
    # Resume via absolute manifest path while passing a different scan_dir —
    # progress must still land under the original scan_dir.
    other = tmp_path / "other_scan"
    other.mkdir()
    report2 = svc.push_scan(other, resume=str(manifest), dry_run=False)
    assert calls["n"] == 1
    assert report2["summary"]["failed"] == 0
    assert report2["summary"]["succeeded"] == 1
    assert Path(report2["manifest"]).resolve() == manifest.resolve()
    assert (scan / "catalog_push_runs" / run_id / "manifest.json").is_file()
    assert not (other / "catalog_push_runs").exists() or not any(
        (other / "catalog_push_runs").glob(f"{run_id}/**")
    )
    reloaded = json.loads(manifest.read_text(encoding="utf-8"))
    assert reloaded.get("om_version") == "1.11.13"
    assert reloaded.get("contract_strategy") == "native"
