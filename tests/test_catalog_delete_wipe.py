"""Tests for ``redibis catalog delete`` / ``catalog wipe`` planning + safety."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from redibis.config import CatalogConfig, OpenMetadataCatalogConfig
from redibis.services.catalog.openmetadata import OpenMetadataPublisher, OpenMetadataSettings
from redibis.services.catalog_service import CatalogService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


@pytest.fixture
def publisher() -> OpenMetadataPublisher:
    return OpenMetadataPublisher(OpenMetadataSettings(
        host="http://localhost:8585",
        jwt="",
        service_name="redibis",
        default_schema="default",
    ))


def test_managed_table_fqn(publisher):
    assert publisher.managed_table_fqn("golden.tutorial_customers") == (
        "redibis.golden.default.tutorial_customers"
    )


def test_delete_tables_dry_run_plan(publisher):
    plan = publisher.delete_tables(
        ["golden.tutorial_customers"],
        fqns=["redibis.other.default.sample"],
        dry_run=True,
    )
    assert plan["dry_run"] is True
    assert plan["tables"] == [
        "redibis.golden.default.tutorial_customers",
        "redibis.other.default.sample",
    ]
    assert all(a["kind"] == "table" for a in plan["actions"])
    assert "results" not in plan


def test_delete_tables_requires_target(publisher):
    with pytest.raises(ValueError, match="TABLE|--fqn"):
        publisher.delete_tables(dry_run=True)


def test_wipe_dry_run_plan(publisher):
    plan = publisher.wipe(
        dry_run=True,
        with_glossary=True,
        with_classifications=True,
    )
    assert plan["dry_run"] is True
    assert plan["service_name"] == "redibis"
    kinds = [a["kind"] for a in plan["actions"]]
    assert kinds[0] == "databaseService"
    assert "glossary" in kinds
    assert kinds.count("classification") == 2


def test_catalog_service_delete_and_wipe_delegate(tmp_path, publisher, monkeypatch):
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    cfg = CatalogConfig(
        backend="openmetadata",
        openmetadata=OpenMetadataCatalogConfig(service_name="redibis", default_schema="default"),
    )
    svc = CatalogService(store=store, config=cfg)

    calls = {}

    def _fake_publisher(backend=None):
        return publisher

    def _delete_tables(**kwargs):
        calls["delete"] = kwargs
        return {"dry_run": True, "tables": ["x"]}

    def _wipe(**kwargs):
        calls["wipe"] = kwargs
        return {"dry_run": True, "service_name": "redibis"}

    monkeypatch.setattr(svc, "_publisher", _fake_publisher)
    monkeypatch.setattr(publisher, "delete_tables", _delete_tables)
    monkeypatch.setattr(publisher, "wipe", _wipe)

    out = svc.delete_tables(["golden.tutorial_customers"], dry_run=True)
    assert out["tables"] == ["x"]
    assert calls["delete"]["tables"] == ["golden.tutorial_customers"]

    out = svc.wipe_catalog(dry_run=True, with_glossary=True)
    assert out["service_name"] == "redibis"
    assert calls["wipe"]["with_glossary"] is True


def test_cli_delete_requires_yes_for_execute(tmp_path, monkeypatch, capsys):
    from redibis.cli.catalog_cmd import _catalog_delete

    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    cfg = CatalogConfig(backend="openmetadata")
    svc = CatalogService(store=store, config=cfg)

    seen = {}

    def _fake_delete(**kwargs):
        seen.update(kwargs)
        return {
            "dry_run": kwargs["dry_run"],
            "tables": ["redibis.golden.default.tutorial_customers"],
            "actions": [],
        }

    monkeypatch.setattr(svc, "delete_tables", _fake_delete)
    args = SimpleNamespace(
        tables=["golden.tutorial_customers"],
        table=None,
        fqn=[],
        yes=False,
        soft=False,
        json=True,
        backend=None,
    )
    rc = _catalog_delete(args, svc)
    assert rc == 0
    assert seen["dry_run"] is True
    err = capsys.readouterr().err
    assert "--yes" in err


def test_cli_wipe_help_lists_flags():
    from redibis.cli.main import main
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            main(["catalog", "wipe", "--help"])
        except SystemExit as e:
            assert e.code in (0, None)
    help_text = buf.getvalue()
    assert "--yes" in help_text
    assert "--with-glossary" in help_text
    assert "--with-classifications" in help_text
    assert "--service" in help_text
