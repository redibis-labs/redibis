"""OpenMetadata table FQN resolution (lookup / search / create-if-missing)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol
from urllib.parse import quote

from redibis.services.catalog.mapping import split_physical_name


class _ResolveClient(Protocol):
    def get_table_or_none(self, fqn: str, *, fields: str = "") -> Optional[dict]: ...

    def search_tables(self, query: str, *, size: int = 50) -> list[dict]: ...


class _ResolveSettings(Protocol):
    service_name: str
    default_schema: str
    fqn_map: dict
    search_services: list


class _EntityCache(Protocol):
    def get(self, table: str, backend: str) -> Optional[dict]: ...

    def put(
        self,
        table: str,
        backend: str,
        *,
        target_fqn: str,
        is_redibis_managed: bool,
        resolved_at: Optional[str] = None,
    ) -> dict: ...

    def invalidate(self, table: str, backend: str) -> None: ...


def _candidate_fqns(table: str, cfg: _ResolveSettings) -> list[str]:
    database, table_name = split_physical_name(table)
    services: list[str] = []
    for svc in list(cfg.search_services or []) + [cfg.service_name]:
        if svc and svc not in services:
            services.append(svc)
    out: list[str] = []
    seen: set[str] = set()
    for svc in services:
        for schema in (cfg.default_schema, database):
            if not schema:
                continue
            fqn = f"{svc}.{database}.{schema}.{table_name}"
            if fqn not in seen:
                seen.add(fqn)
                out.append(fqn)
    return out


def _managed_fqn(table: str, cfg: _ResolveSettings) -> str:
    database, table_name = split_physical_name(table)
    return f"{cfg.service_name}.{database}.{cfg.default_schema}.{table_name}"


def _is_under_service(fqn: str, service_name: str) -> bool:
    return bool(service_name) and fqn.startswith(f"{service_name}.")


def _filter_search_hits(
    hits: list[dict],
    *,
    database: str,
    table_name: str,
) -> list[str]:
    matches: list[str] = []
    for hit in hits:
        src = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
        if not isinstance(src, dict):
            continue
        name = str(src.get("name") or "")
        db = src.get("database")
        if isinstance(db, dict):
            db_name = str(db.get("name") or "")
        else:
            db_name = str(db or "")
        fqn = str(src.get("fullyQualifiedName") or "")
        if name == table_name and db_name == database and fqn:
            if fqn not in matches:
                matches.append(fqn)
    return matches


def _parse_resolved_at(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cache_entry_fresh(
    cached: dict,
    *,
    ttl_hours: int,
    now: Optional[datetime] = None,
) -> bool:
    """True when ``resolved_at`` is within ``ttl_hours`` (missing → treat as stale)."""
    if ttl_hours <= 0:
        return False
    resolved = _parse_resolved_at(cached.get("resolved_at"))
    if resolved is None:
        return False
    now = now or datetime.now(timezone.utc)
    return resolved + timedelta(hours=ttl_hours) > now


def resolve_table_fqn(
    client: _ResolveClient,
    table: str,
    cfg: _ResolveSettings,
    cache: Optional[_EntityCache] = None,
    *,
    backend: str = "openmetadata",
    refresh: bool = False,
    now: Optional[datetime] = None,
) -> tuple[str, bool]:
    """Return ``(target_fqn, is_redibis_managed)``.

    Order: explicit ``fqn_map`` → cache (TTL-gated) → direct GET candidates →
    search → create-if-missing / error per ``entity_mode``.

    Pass ``refresh=True`` (CLI ``--refresh-resolution``) to bypass and invalidate
    a stale cache entry — e.g. after a table was re-ingested under a different
    service. On a TTL-fresh cache hit, verify with ``get_table_or_none``; a 404
    invalidates and re-resolves once.
    """
    mode = (
        getattr(cfg, "entity_mode", None)
        or getattr(cfg, "mode", None)
        or "mixed"
    ).lower()
    fqn_map = getattr(cfg, "fqn_map", None) or {}
    ttl_hours = int(getattr(cfg, "resolve_cache_ttl_hours", 168) or 168)
    now = now or datetime.now(timezone.utc)

    if refresh and cache is not None:
        invalidate = getattr(cache, "invalidate", None)
        if callable(invalidate):
            invalidate(table, backend)

    # 1. Explicit mapping always wins
    if table in fqn_map:
        fqn = str(fqn_map[table])
        managed = _is_under_service(fqn, cfg.service_name)
        if cache is not None:
            cache.put(
                table, backend, target_fqn=fqn, is_redibis_managed=managed,
            )
        return fqn, managed

    # 2. Cache hit (skipped when refresh=True); TTL miss → fall through
    if cache is not None and not refresh:
        cached = cache.get(table, backend)
        if cached and cached.get("target_fqn") and _cache_entry_fresh(
            cached, ttl_hours=ttl_hours, now=now,
        ):
            cached_fqn = str(cached["target_fqn"])
            cached_managed = bool(cached.get("is_redibis_managed", False))
            # Cheap verify: 404 → invalidate and re-resolve once
            entity = client.get_table_or_none(cached_fqn, fields="")
            if entity is not None:
                return cached_fqn, cached_managed
            invalidate = getattr(cache, "invalidate", None)
            if callable(invalidate):
                invalidate(table, backend)
            # Fall through to fresh resolution below (one re-resolve).

    database, table_name = split_physical_name(table)

    # 3. Direct GET candidates
    for candidate in _candidate_fqns(table, cfg):
        entity = client.get_table_or_none(candidate, fields="columns,tags")
        if entity:
            fqn = str(
                entity.get("fullyQualifiedName") or candidate
            )
            managed = _is_under_service(fqn, cfg.service_name)
            if cache is not None:
                cache.put(
                    table, backend, target_fqn=fqn, is_redibis_managed=managed,
                )
            return fqn, managed

    # 4. Search
    hits = client.search_tables(table_name, size=50)
    matches = _filter_search_hits(hits, database=database, table_name=table_name)
    if len(matches) == 1:
        fqn = matches[0]
        managed = _is_under_service(fqn, cfg.service_name)
        if cache is not None:
            cache.put(
                table, backend, target_fqn=fqn, is_redibis_managed=managed,
            )
        return fqn, managed
    if len(matches) > 1:
        raise RuntimeError(
            f"Ambiguous OpenMetadata table resolution for {table!r}: "
            f"{matches}. Set catalog.openmetadata.fqn_map[{table!r}] explicitly."
        )

    # 5. Zero matches
    if mode in ("create_if_missing", "mixed"):
        fqn = _managed_fqn(table, cfg)
        if cache is not None:
            cache.put(
                table, backend, target_fqn=fqn, is_redibis_managed=True,
            )
        return fqn, True

    raise RuntimeError(
        f"No OpenMetadata table found for {table!r} "
        f"(entity_mode={mode!r}; set fqn_map or use create_if_missing/mixed)."
    )


def encode_table_name_path(fqn: str) -> str:
    """URL-encode a dotted FQN for ``/v1/tables/name/{fqn}``."""
    return quote(fqn, safe="")


__all__ = [
    "encode_table_name_path",
    "resolve_table_fqn",
]
