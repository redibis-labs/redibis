"""Entity FQN resolution cache refresh + TTL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from redibis.services.catalog.openmetadata_resolve import resolve_table_fqn
from redibis.store.catalog_ledger import EntityResolutionCache
from redibis.store.storage_backend import LocalBackend


NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


class _Cfg:
    service_name = "redibis"
    default_schema = "default"
    entity_mode = "enrich_existing"
    fqn_map: dict = {}
    search_services: list = []
    resolve_cache_ttl_hours: int = 168


def test_resolve_refresh_bypasses_stale_cache(tmp_path):
    storage = LocalBackend(str(tmp_path / "store"))
    bucket = "contracts"
    cache = EntityResolutionCache(storage, bucket)
    table = "telecom.customers"
    cache.put(
        table,
        "openmetadata",
        target_fqn="old_svc.telecom.public.customers",
        is_redibis_managed=False,
        resolved_at=NOW.isoformat(),
    )

    class Client:
        def __init__(self):
            self.gets: list[str] = []

        def get_table_or_none(self, fqn, *, fields=""):
            self.gets.append(fqn)
            if fqn.endswith("telecom.default.customers") or "redibis.telecom" in fqn:
                return {"fullyQualifiedName": "redibis.telecom.default.customers"}
            if fqn.startswith("old_svc."):
                return {"fullyQualifiedName": fqn}  # verify hit still finds it
            return None

        def search_tables(self, query, *, size=50):
            return []

    client = Client()
    # Without refresh: TTL-fresh cache → verify GET, then return cached FQN
    fqn, managed = resolve_table_fqn(
        client, table, _Cfg(), cache=cache, backend="openmetadata", now=NOW,
    )
    assert fqn == "old_svc.telecom.public.customers"
    assert client.gets == ["old_svc.telecom.public.customers"]

    # With refresh: invalidate + re-lookup
    client.gets.clear()
    fqn2, managed2 = resolve_table_fqn(
        client, table, _Cfg(), cache=cache, backend="openmetadata",
        refresh=True, now=NOW,
    )
    assert fqn2 == "redibis.telecom.default.customers"
    assert managed2 is True
    assert client.gets  # actually hit OM
    # Cache updated
    cached = cache.get(table, "openmetadata")
    assert cached is not None
    assert cached["target_fqn"] == "redibis.telecom.default.customers"


def test_resolve_cache_ttl_expiry_is_miss(tmp_path):
    storage = LocalBackend(str(tmp_path / "store"))
    cache = EntityResolutionCache(storage, "contracts")
    table = "telecom.customers"
    stale_at = (NOW - timedelta(hours=200)).isoformat()
    cache.put(
        table,
        "openmetadata",
        target_fqn="old_svc.telecom.public.customers",
        is_redibis_managed=False,
        resolved_at=stale_at,
    )

    class Client:
        def __init__(self):
            self.gets: list[str] = []

        def get_table_or_none(self, fqn, *, fields=""):
            self.gets.append(fqn)
            if "redibis.telecom.default.customers" in fqn:
                return {"fullyQualifiedName": "redibis.telecom.default.customers"}
            return None

        def search_tables(self, query, *, size=50):
            return []

    client = Client()
    cfg = _Cfg()
    cfg.resolve_cache_ttl_hours = 168
    fqn, managed = resolve_table_fqn(
        client, table, cfg, cache=cache, backend="openmetadata", now=NOW,
    )
    assert fqn == "redibis.telecom.default.customers"
    assert managed is True
    # Expired cache skipped verify-on-hit; went to candidate GETs
    assert any("redibis.telecom" in g for g in client.gets)


def test_resolve_cache_hit_404_invalidates_and_reresolves(tmp_path):
    storage = LocalBackend(str(tmp_path / "store"))
    cache = EntityResolutionCache(storage, "contracts")
    table = "telecom.customers"
    cache.put(
        table,
        "openmetadata",
        target_fqn="gone_svc.telecom.public.customers",
        is_redibis_managed=False,
        resolved_at=NOW.isoformat(),
    )

    class Client:
        def __init__(self):
            self.gets: list[str] = []

        def get_table_or_none(self, fqn, *, fields=""):
            self.gets.append(fqn)
            if fqn.startswith("gone_svc."):
                return None  # 404
            if "redibis.telecom.default.customers" in fqn:
                return {"fullyQualifiedName": "redibis.telecom.default.customers"}
            return None

        def search_tables(self, query, *, size=50):
            return []

    client = Client()
    fqn, managed = resolve_table_fqn(
        client, table, _Cfg(), cache=cache, backend="openmetadata", now=NOW,
    )
    assert fqn == "redibis.telecom.default.customers"
    assert managed is True
    assert client.gets[0] == "gone_svc.telecom.public.customers"
    cached = cache.get(table, "openmetadata")
    assert cached is not None
    assert cached["target_fqn"] == "redibis.telecom.default.customers"
