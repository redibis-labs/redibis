"""CLI handlers for ``redibis memory`` (column memory / enrichment learning loop)."""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

from redibis.config import RedibisConfig
from redibis.memory.retriever import get_context_retriever, hint_to_dict
from redibis.memory.async_writer import memory_write_stats
from redibis.memory.store import InMemoryMemoryStore, MemoryListEntry, get_memory_store
from redibis.memory.writer import fingerprint_from_column_prop
from redibis.store.contract_store import ContractStore


def _load_config(args) -> RedibisConfig:
    path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
    if path:
        return RedibisConfig.from_yaml(path)
    return RedibisConfig.default()


def _memory_store_for(store: ContractStore, mc):
    attached = getattr(store, "_memory_store", None)
    if attached is not None:
        return attached
    return get_memory_store(mc)


def _memory_row_count(store) -> Optional[int]:
    if isinstance(store, InMemoryMemoryStore):
        return len(store._rows)
    conn = getattr(store, "_conn", None)
    if conn is None:
        try:
            conn = store._connect()
        except Exception:
            return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM column_memory")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return None


def _serialize_hint(ctx) -> dict:
    return hint_to_dict(ctx)


def _latest_decision(entry: MemoryListEntry) -> dict:
    return entry.decisions[-1] if entry.decisions else {}


def _workflows(entry: MemoryListEntry) -> set[str]:
    out: set[str] = set()
    for d in entry.decisions:
        wf = (d.get("provenance") or {}).get("workflow")
        if wf:
            out.add(str(wf))
    return out


def _tables(entry: MemoryListEntry) -> set[str]:
    return {str(d["table"]) for d in entry.decisions if d.get("table")}


def _serialize_entry(entry: MemoryListEntry) -> dict:
    latest = _latest_decision(entry)
    fp = entry.fingerprint or {}
    prov = latest.get("provenance") or {}
    return {
        "canonical_key": entry.canonical_key,
        "column": fp.get("name_normalized") or fp.get("column") or "",
        "logical_type": entry.logical_type or fp.get("logical_type") or "",
        "format_signature": entry.format_signature or fp.get("format_signature") or "",
        "domain": entry.domain or fp.get("domain") or "",
        "entity_type": entry.entity_type or fp.get("entity_type"),
        "occurrence_count": entry.occurrence_count,
        "decision_count": len(entry.decisions),
        "updated_at": entry.updated_at,
        "table": latest.get("table") or "",
        "source_column": latest.get("column") or "",
        "workflow": prov.get("workflow") or "",
        "reviewer": latest.get("reviewer") or "",
        "contract_version": latest.get("contract_version") or "",
        "pii_verdict": latest.get("pii_verdict") or "",
        "classification": latest.get("classification") or "",
        "business_definition": latest.get("business_definition") or "",
        "quality_rules": len(latest.get("quality_rules") or []),
        "rationale": latest.get("rationale") or "",
        "workflows": sorted(_workflows(entry)),
        "tables": sorted(_tables(entry)),
    }


def _filter_entries(
    entries: list[MemoryListEntry],
    *,
    table: str = "",
    workflow: str = "",
) -> list[MemoryListEntry]:
    out: list[MemoryListEntry] = []
    for entry in entries:
        if table and table not in _tables(entry):
            continue
        if workflow and workflow not in _workflows(entry):
            continue
        out.append(entry)
    return out


def run_memory(args, store: ContractStore) -> int:
    action = args.memory_action
    cfg = _load_config(args)
    mc = cfg.memory

    if action == "init-db":
        if not mc.enabled:
            print("memory.enabled is false — enable memory in config or use --enable-memory", file=sys.stderr)
            return 1
        mem_store = _memory_store_for(store, mc)
        if mem_store is None:
            print("Could not open memory store", file=sys.stderr)
            return 1
        mem_store.ensure_schema()
        print(f"Memory schema ready (store={mc.store}).")
        return 0

    if action == "status":
        if getattr(args, "json", False):
            payload = {
                "enabled": mc.enabled,
                "store": mc.store,
                "domain": mc.domain,
                "embedding_provider": mc.embedding_provider,
                "top_k": mc.top_k,
                "min_similarity": mc.min_similarity,
                "dsn_ref": mc.dsn_ref or None,
                "config": getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG"),
            }
            if mc.enabled:
                mem_store = _memory_store_for(store, mc)
                payload["store_ready"] = mem_store is not None
                if mem_store is not None:
                    payload["row_count"] = _memory_row_count(mem_store)
                payload["async_writes"] = mc.async_writes
                payload["write_stats"] = memory_write_stats().to_dict()
            print(json.dumps(payload, indent=2))
            return 0
        cfg_path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
        if cfg_path:
            print(f"Config: {cfg_path}")
        else:
            print("Config: (built-in defaults — set REDIBIS_CONFIG or --config)")
        print(f"enabled: {mc.enabled}")
        print(f"store: {mc.store}")
        print(f"domain: {mc.domain or '(unset)'}")
        print(f"embedding: {mc.embedding_provider}")
        if not mc.enabled:
            return 0
        mem_store = _memory_store_for(store, mc)
        if mem_store is None:
            print("store: not available")
            return 1
        count = _memory_row_count(mem_store)
        print(f"rows: {count if count is not None else '?'}")
        stats = memory_write_stats()
        print(
            f"async queue: pending={stats.queue_pending} "
            f"submitted={stats.tasks_submitted} failed={stats.tasks_failed}"
        )
        print(
            f"writes: ok={stats.writes_succeeded} failed={stats.writes_failed}"
        )
        return 0

    if action == "list":
        if not mc.enabled:
            print("memory.enabled is false", file=sys.stderr)
            return 1
        mem_store = _memory_store_for(store, mc)
        if mem_store is None:
            print("Memory store unavailable", file=sys.stderr)
            return 1
        limit = int(getattr(args, "limit", 500) or 500)
        table_filter = (getattr(args, "table", None) or "").strip()
        workflow_filter = (getattr(args, "workflow", None) or "").strip()
        entries = _filter_entries(
            mem_store.list_entries(limit=limit),
            table=table_filter,
            workflow=workflow_filter,
        )
        rows = [_serialize_entry(e) for e in entries]
        if getattr(args, "json", False):
            print(json.dumps({"count": len(rows), "entries": rows}, indent=2))
            return 0
        if not rows:
            print("No column memory entries found.")
            return 0
        for row in rows:
            wf = row["workflow"] or (row["workflows"][0] if row["workflows"] else "")
            bits = [
                row["logical_type"] or "?",
                f"format={row['format_signature'] or '?'}",
                f"occurrences={row['occurrence_count']}",
            ]
            if row["pii_verdict"]:
                bits.append(f"PII={row['pii_verdict']}")
            if row["quality_rules"]:
                bits.append(f"quality_rules={row['quality_rules']}")
            if row["business_definition"]:
                bits.append(f"definition={row['business_definition'][:80]}")
            if wf:
                bits.append(f"via {wf}")
            tbl = row["table"] or (row["tables"][0] if row["tables"] else "")
            src = f"{tbl}.{row['source_column']}" if tbl and row["source_column"] else tbl
            print(f"{row['column'] or row['canonical_key']}\t{src}\t" + " · ".join(bits))
            if row["rationale"]:
                print(f"  {row['rationale']}")
        return 0

    if action == "search":
        if not mc.enabled:
            print("memory.enabled is false", file=sys.stderr)
            return 1
        table = (args.table or "").strip()
        if not table:
            print("search requires TABLE (schema.table)", file=sys.stderr)
            return 2
        active = store.get_active(table)
        if active is None:
            print(f"No active contract for {table!r}", file=sys.stderr)
            return 1
        mem_store = _memory_store_for(store, mc)
        retriever = get_context_retriever(mc, memory_store=mem_store)
        if retriever is None:
            print("Memory retriever unavailable", file=sys.stderr)
            return 1
        column_filter = (getattr(args, "column", None) or "").strip()
        results: list[dict] = []
        for schema_obj in active.get("schema", []) or []:
            for prop in schema_obj.get("properties", []) or []:
                if not isinstance(prop, dict):
                    continue
                col = prop.get("name")
                if not col:
                    continue
                if column_filter and col != column_filter:
                    continue
                fp = fingerprint_from_column_prop(
                    table, str(col), prop, domain=mc.domain,
                )
                hints = retriever.retrieve(fp)
                if not hints:
                    continue
                results.append({
                    "column": col,
                    "hints": [_serialize_hint(h) for h in hints],
                })
        if getattr(args, "json", False):
            print(json.dumps({"table": table, "columns": results}, indent=2))
            return 0
        if not results:
            print(f"No similar past reviews for {table}")
            return 0
        for block in results:
            print(f"\n## {block['column']}")
            for hint in block["hints"]:
                fp = hint.get("fingerprint") or {}
                print(
                    f"  similarity={hint['similarity']:.3f} "
                    f"format={fp.get('format_signature', '')} "
                    f"occurrences={hint.get('occurrence_count', 1)}"
                )
                if hint.get("pii_verdict"):
                    print(f"    PII: {hint['pii_verdict']}")
                if hint.get("business_definition"):
                    print(f"    definition: {hint['business_definition']}")
                if hint.get("rationale"):
                    print(f"    rationale: {hint['rationale']}")
        return 0

    return 2
