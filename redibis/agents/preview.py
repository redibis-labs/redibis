"""Pre-run scope estimate — cheap metadata, no scan (Phase 4 T4.3)."""

from __future__ import annotations

from typing import Any, Optional

from redibis.agents.models import PipelineSpec
from redibis.config import RedibisConfig
from redibis.telemetry.pii_scope import column_has_pii_markers


def _resolve_tables(
    spec: PipelineSpec,
    *,
    tables: Optional[list[str]] = None,
    database: str = "",
    contract_store: Any = None,
) -> list[str]:
    if tables:
        return list(tables)

    batch = next((n for n in spec.nodes if n.kind in ("batch", "batch_loop")), None)
    db_filter = database or (batch.params.get("database") if batch else "") or ""

    if contract_store is not None:
        listed = contract_store.list_tables()
        if db_filter:
            prefix = f"{db_filter}."
            listed = [t for t in listed if t.startswith(prefix)]
        if listed:
            return sorted(listed)

    source = next(
        (n for n in spec.nodes if n.kind in ("source", "source_table")),
        None,
    )
    if source and source.params.get("table"):
        return [str(source.params["table"])]

    return []


def _estimate_table(
    table: str,
    *,
    contract_store: Any = None,
    config: RedibisConfig,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "table": table,
        "columns": 0,
        "likely_pii_columns": [],
        "has_contract": False,
        "source": "unknown",
    }

    if contract_store is not None:
        contract = contract_store.get_active(table)
        if contract:
            out["has_contract"] = True
            out["source"] = "active_contract"
            props: list[dict] = []
            for schema in contract.get("schema") or []:
                phys = str(schema.get("physicalName") or "")
                if phys and phys != table and not table.endswith(phys.split(".")[-1]):
                    continue
                props.extend(schema.get("properties") or [])
            if not props and contract.get("schema"):
                props = (contract["schema"][0] or {}).get("properties") or []
            out["columns"] = len(props)
            out["likely_pii_columns"] = [
                str(p.get("name"))
                for p in props
                if p.get("name") and column_has_pii_markers(p)
            ]
            return out

    engine = (config.source.engine or "none").lower()
    if engine in ("hive", "postgres", "oracle", "jdbc"):
        try:
            from redibis.metadata import get_metadata_retriever

            retriever = get_metadata_retriever(config.source)
            meta = retriever.get_source_metadata(table)
            out["source"] = f"metadata:{engine}"
            out["columns"] = len(meta.columns)
            out["likely_pii_columns"] = _heuristic_pii_columns(meta.columns)
            return out
        except Exception as exc:
            out["metadata_error"] = str(exc)

    return out


def _heuristic_pii_columns(columns: list[Any]) -> list[str]:
    """Name-based PII hints when no contract exists."""
    hints = ("email", "phone", "msisdn", "ssn", "name", "address", "ip", "imei", "imsi")
    found: list[str] = []
    for col in columns:
        name = str(getattr(col, "name", col) or "").lower()
        if any(h in name for h in hints):
            found.append(str(getattr(col, "name", col)))
    return found


def estimate_pipeline_scope(
    spec: PipelineSpec,
    *,
    config: Optional[RedibisConfig] = None,
    contract_store: Any = None,
    tables: Optional[list[str]] = None,
    database: str = "",
    try_on_n: int = 0,
) -> dict[str, Any]:
    """
    Estimate tables/columns/likely-PII before a run.

    ``try_on_n`` limits the preview to N tables (0 = all resolved tables).
    """
    cfg = config or RedibisConfig.default()
    resolved = _resolve_tables(
        spec,
        tables=tables,
        database=database,
        contract_store=contract_store,
    )
    if not resolved:
        return {
            "tables_total": 0,
            "tables": [],
            "message": "no tables resolved — configure source/batch node or pass tables",
        }

    preview_tables = resolved[:try_on_n] if try_on_n > 0 else resolved
    estimates = [
        _estimate_table(t, contract_store=contract_store, config=cfg)
        for t in preview_tables
    ]
    pii_total = sum(len(e.get("likely_pii_columns") or []) for e in estimates)
    col_total = sum(int(e.get("columns") or 0) for e in estimates)

    integrations: list[str] = []
    for node in spec.nodes:
        kind = node.kind
        if kind in ("publish", "catalog_push"):
            integrations.append(str(node.params.get("backend") or "catalog"))
        if kind in ("source", "source_table"):
            integrations.append(str(node.params.get("engine") or cfg.source.engine))

    return {
        "tables_total": len(resolved),
        "tables_previewed": len(preview_tables),
        "columns_estimated": col_total,
        "likely_pii_columns": pii_total,
        "integrations": sorted(set(i for i in integrations if i)),
        "tables": estimates,
        "try_on_n": try_on_n,
        "can_run_all": len(resolved) <= 50,
    }
