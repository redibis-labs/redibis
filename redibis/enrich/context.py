"""
Enrichment context bundle — assembles and stores what the LLM sees (§5A).

Per-run edits are stored under the contracts bucket (draft key); run artifacts
still go through RunOutputWriter only.

Fresh bundles are built via ``EnrichmentService.build_context`` so the UI review
payload matches the LLM prompt exactly.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from redibis.enrich.service import EnrichmentService


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_columns(contract: dict):
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict) and prop.get("name"):
                yield prop


def _prop_by_name(contract: dict, name: str) -> Optional[dict]:
    for prop in _iter_columns(contract):
        if prop.get("name") == name:
            return prop
    return None


def _column_ui_mirrors(col_ctx: dict, prop: dict) -> dict:
    """Add steward-editable top-level fields mirroring deterministic_verdict."""
    from redibis.contracts.privacy import column_is_pii, col_pii_engine

    verdict = col_ctx.get("deterministic_verdict") or {}
    ce = col_pii_engine(prop) if prop else {}
    return {
        **col_ctx,
        "classification": prop.get("classification") or verdict.get("classification"),
        "entity_type": ce.get("entity_type") or verdict.get("entity_type") or prop.get("entity_type"),
        "tags": list(prop.get("tags") or verdict.get("tags") or []),
        "is_pii": column_is_pii(prop) if prop else bool(verdict.get("classification")),
    }


def _apply_column_slice(prop: dict, slice_: dict) -> None:
    """Apply editable column fields from a context slice onto a contract property."""
    verdict = slice_.get("deterministic_verdict") or {}
    classification = slice_.get("classification")
    if classification is None:
        classification = verdict.get("classification")
    if classification is not None:
        prop["classification"] = classification

    for key in ("businessName", "logicalType", "physicalType"):
        if key in slice_ and slice_[key] is not None:
            prop[key] = slice_[key]
    if "tags" in slice_ and slice_["tags"] is not None:
        prop["tags"] = list(slice_["tags"])
    if slice_.get("business") is not None:
        prop["business"] = copy.deepcopy(slice_["business"])
    if slice_.get("privacy") is not None:
        prop["privacy"] = copy.deepcopy(slice_["privacy"])
    if slice_.get("quality") is not None:
        prop["quality"] = copy.deepcopy(slice_["quality"])
    entity = slice_.get("entity_type") or verdict.get("entity_type")
    if entity:
        from redibis.contracts.privacy import apply_privacy_to_column

        payload: dict[str, Any] = {}
        if classification:
            payload["classification"] = classification
        payload["pii"] = {"detected": True, "entity_type": entity}
        apply_privacy_to_column(prop, payload)


def contract_from_bundle(bundle: dict) -> dict:
    """Rebuild C_det from bundle contract_det + per-column edits."""
    contract = copy.deepcopy(bundle.get("contract_det") or {})
    edits = {
        item.get("name"): item
        for item in (bundle.get("columns") or [])
        if item.get("name")
    }
    for prop in _iter_columns(contract):
        col = prop.get("name")
        if col and col in edits:
            _apply_column_slice(prop, edits[col])
    return contract


def context_bundle_from_contract(
    contract: dict,
    table: str,
    *,
    run_id: Optional[str] = None,
) -> dict:
    """Minimal context bundle seeded from an on-disk ODCS contract."""
    return {
        "table": table,
        "run_id": run_id or _new_run_id(),
        "contract_det": copy.deepcopy(contract),
        "columns": [],
    }


def load_contract_for_enrichment(
    path: Union[str, Path],
    *,
    table: Optional[str] = None,
) -> tuple[dict, str]:
    """Load a YAML/JSON contract file and resolve ``schema.table`` for enrichment."""
    from redibis.services.catalog.mapping import load_contract_file, resolve_contract_table

    contract = load_contract_file(path)
    resolved = resolve_contract_table(contract, override=table, path=path)
    return contract, resolved


def _flatten_similar_columns(column_context: list[dict]) -> list[dict]:
    """Table-level similar-column list for bundle metadata (legacy UI field)."""
    flat: list[dict] = []
    for col_ctx in column_context:
        source = col_ctx.get("name")
        for row in col_ctx.get("similar_columns") or []:
            flat.append({**row, "column": source})
    flat.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    return flat


def assemble_enrichment_context(
    service: "EnrichmentService",
    table: str,
    *,
    redibis_config=None,
    run_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    similar_context_enabled: Optional[bool] = None,
    similar_context_k: Optional[int] = None,
    provider_name: str = "demo",
) -> dict:
    """Build the enrichment-context bundle for review or auto-run."""
    from redibis.config import RedibisConfig
    from redibis.enrich.prompt_store import resolve_enrichment_system_prompt
    from redibis.enrich.providers import get_provider

    cfg = redibis_config or RedibisConfig()

    draft = load_context_draft(service.store, table)
    if draft and draft.get("table") == table:
        bundle = copy.deepcopy(draft)
        bundle["run_id"] = run_id or bundle.get("run_id") or _new_run_id()
        return bundle

    provider = get_provider(provider_name)
    ctx = service.build_context(
        table,
        provider,
        redibis_config=cfg,
        system_prompt=system_prompt,
        extra_instructions=extra_instructions,
        run_id=run_id,
        similar_context_enabled=similar_context_enabled,
        similar_context_k=similar_context_k,
    )

    memory_hints: list[dict] = []
    if ctx.memory_context:
        memory_hints.append({"kind": "memory_section", "text": ctx.memory_context})

    sc_cfg = cfg.enrich.similar_context
    use_similar = (
        similar_context_enabled
        if similar_context_enabled is not None
        else sc_cfg.enabled
    )
    sc_k = similar_context_k if similar_context_k is not None else sc_cfg.k

    columns = [
        _column_ui_mirrors(col_ctx, _prop_by_name(ctx.c_det, str(col_ctx.get("name") or "")) or {})
        for col_ctx in ctx.column_context
    ]

    instructions = {
        "system_prompt": system_prompt or resolve_enrichment_system_prompt(),
        "extra_instructions": extra_instructions or "",
        "edge_rules": _edge_rules_summary(cfg),
        "policy_pack": getattr(cfg.classification, "policy_pack", "telecom"),
    }

    return {
        "table": table,
        "run_id": ctx.run_id,
        "assembled_at": _utc_now_iso(),
        "contract_det": copy.deepcopy(ctx.c_det),
        "columns": columns,
        "contract_for_prompt": copy.deepcopy(ctx.contract_for_prompt),
        "instructions": instructions,
        "docs": {
            "context_docs": service.list_context_docs(table),
            "example_docs": service.list_example_docs(table),
            "sample_data": service.list_sample_data(table),
            "example_contracts": [],
            "pack_digest": copy.deepcopy(ctx.pack_digest),
        },
        "memory_hints": memory_hints,
        "similar_columns": _flatten_similar_columns(columns),
        "global": {
            "include_pii_evidence": bool(cfg.enrich.include_pii_evidence),
            "sample_policy": ctx.sample_policy,
            "similar_context": {
                "enabled": use_similar,
                "k": sc_k if use_similar else 0,
            },
        },
    }


def preview_enrichment_prompt(
    service: "EnrichmentService",
    bundle: dict,
    *,
    redibis_config=None,
    example_contracts: Optional[list[str]] = None,
    provider_name: str = "demo",
) -> dict:
    """Render the exact system + user prompts (no LLM call) via shared build_context."""
    from redibis.config import RedibisConfig
    from redibis.enrich.providers import get_provider

    cfg = redibis_config or RedibisConfig()
    table = bundle["table"]
    provider = get_provider(provider_name)
    global_cfg = bundle.get("global") or {}
    sc_cfg = global_cfg.get("similar_context") or {}
    ctx = service.build_context(
        table,
        provider,
        redibis_config=cfg,
        context_bundle=bundle,
        example_contracts=example_contracts or bundle.get("docs", {}).get("example_contracts"),
        similar_context_enabled=sc_cfg.get("enabled"),
        similar_context_k=sc_cfg.get("k") or None,
        sample_policy=global_cfg.get("sample_policy"),
    )
    return {
        "table": ctx.table,
        "run_id": ctx.run_id,
        "system_prompt": ctx.system_prompt,
        "user_prompt": ctx.user_prompt,
        "prompt_chars": len(ctx.system_prompt) + len(ctx.user_prompt),
        "contract_for_prompt": ctx.contract_for_prompt,
    }


def save_context_draft(store, table: str, bundle: dict) -> str:
    """Persist per-run context edits (contracts bucket draft — not active spec)."""
    key = store._enrichment_draft_context_key(table)
    payload = copy.deepcopy(bundle)
    payload["table"] = table
    payload["updated_at"] = _utc_now_iso()
    store.backend.put_json(store.bucket, key, payload)
    return key


def load_context_draft(store, table: str) -> Optional[dict]:
    key = store._enrichment_draft_context_key(table)
    if not store.backend.exists(store.bucket, key):
        return None
    try:
        data = store.backend.get_json(store.bucket, key)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_context_draft(store, table: str) -> None:
    key = store._enrichment_draft_context_key(table)
    if store.backend.exists(store.bucket, key):
        store.backend.delete(store.bucket, key)


def promote_column_to_edge_rule(
    *,
    column: str,
    from_entity: str = "UNKNOWN",
    to_entity: str = "",
    note: str = "",
) -> dict:
    """Build a suggested edge-rule overlay from a steward correction."""
    from redibis.classification.edge_rules import suggest_rule_from_correction

    return suggest_rule_from_correction(
        column=column,
        from_entity=from_entity or "UNKNOWN",
        to_entity=to_entity or "UNKNOWN",
        note=note or f"Promoted from enrichment context review ({column})",
    )


def _edge_rules_summary(cfg) -> list[dict]:
    try:
        from redibis.classification.policy_pack import get_builtin_pack

        pack = get_builtin_pack(cfg.classification.policy_pack)
        rules = pack.edge_rules or []
        return [
            {"id": r.get("id"), "when": r.get("when"), "then": r.get("then")}
            for r in rules[:50]
            if isinstance(r, dict)
        ]
    except Exception:
        return []


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid.uuid4().hex[:8]
