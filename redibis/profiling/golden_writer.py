"""
redibis.profiling.golden_writer — promote approved columns to the golden reference set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.profiling.fingerprint import ColumnFingerprint
from redibis.profiling.golden import GoldenColumn
from redibis.profiling.vector_store import get_vector_store
from redibis.profiling.vectorize import fingerprint_to_vector
from redibis.store.contract_store import ContractStore
from redibis.store.fingerprint_store import FingerprintStore

log = logging.getLogger(__name__)

# Approved basket kinds that reference a promotable column (not table-level rules).
_PROMOTION_KINDS = frozenset({"pii", "quality", "glossary", "business", "manual"})


def _prop_from_contract(contract: dict, column: str) -> Optional[dict]:
    for schema_obj in contract.get("schema") or []:
        for prop in schema_obj.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name") == column:
                return prop
    return None


def _has_structural_fingerprint(fingerprint: Optional[ColumnFingerprint | dict]) -> bool:
    """True when the fingerprint has enough sample-derived signal to embed."""
    if fingerprint is None:
        return False
    if isinstance(fingerprint, ColumnFingerprint):
        return (
            fingerprint.sample_size > 0
            or bool(fingerprint.pattern_masks)
            or fingerprint.distinct_ratio > 0
            or fingerprint.len_mean > 0
        )
    return (
        bool(fingerprint.get("pattern_masks"))
        or int(fingerprint.get("sample_size") or 0) > 0
        or float(fingerprint.get("distinct_ratio") or 0) > 0
        or float(fingerprint.get("len_mean") or 0) > 0
    )


def golden_from_contract_prop(
    table: str,
    column: str,
    prop: dict,
    *,
    fingerprint: Optional[ColumnFingerprint | dict] = None,
    contract_uuid: Optional[str] = None,
    approved_by: str = "system",
) -> Optional[GoldenColumn]:
    """Assemble a GoldenColumn from a merged contract schema property.

    Returns ``None`` when no structural fingerprint is available — a zero vector
    cannot participate in similarity search.
    """
    if not _has_structural_fingerprint(fingerprint):
        return None

    if isinstance(fingerprint, ColumnFingerprint):
        fp_dict = fingerprint.to_dict()
    else:
        fp_dict = dict(fingerprint)  # type: ignore[arg-type]

    emb = fingerprint_to_vector(ColumnFingerprint.from_dict(fp_dict))

    tags = prop.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    privacy = prop.get("privacy") or {}
    pii = prop.get("pii") or {}
    entity_type = prop.get("entity_type") or pii.get("entity_type")
    masking = prop.get("maskingPolicy") or prop.get("masking_policy") or {}

    business = prop.get("business") or {}
    glossary = prop.get("description") or business.get("definition")
    synonyms = business.get("synonyms") or []
    if isinstance(synonyms, str):
        synonyms = [synonyms]

    return GoldenColumn(
        table_name=table,
        column_name=column,
        embedding=emb,
        fingerprint=fp_dict,
        classification=prop.get("classification"),
        entity_type=entity_type,
        tags=list(tags),
        glossary=glossary,
        synonyms=list(synonyms),
        masking_policy=dict(masking) if isinstance(masking, dict) else {},
        privacy=dict(privacy) if isinstance(privacy, dict) else {},
        contract_uuid=contract_uuid,
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )


def _collect_promotion_candidates(
    session: Any,
    contract: dict,
) -> list[tuple[str, dict]]:
    """Unique (column, prop) pairs from all merged approved items."""
    seen: set[str] = set()
    out: list[tuple[str, dict]] = []

    for item in getattr(session, "approved", None).items or []:
        if item.status != "merged":
            continue
        if item.kind not in _PROMOTION_KINDS:
            continue
        col = item.column or (item.payload or {}).get("column") or (item.payload or {}).get("name")
        if not col or col == "__table__":
            continue
        if col in seen:
            continue
        seen.add(col)

        contract_prop = _prop_from_contract(contract, col)
        if item.kind in ("pii", "glossary", "business", "manual") and item.payload:
            prop = {**(contract_prop or {}), **item.payload, "name": col}
        else:
            # quality and other kinds — merged contract is source of truth for governance
            prop = contract_prop or item.payload or {"name": col}
        out.append((col, prop))

    return out


def promote_approved_columns(
    session: Any,
    store: ContractStore,
    *,
    approved_by: str = "system",
) -> int:
    """After merge_approved: upsert each approved column into golden store + vector index.

    Promotes columns from every approved kind (PII, quality, glossary, …), not
    PII-only — so non-PII governed columns like ``customer_id`` enter the golden set.
    """
    table = getattr(session, "table_name", "")
    if not table:
        return 0

    fp_by_col: dict[str, dict] = {}
    profile = getattr(session, "profiler", None) or getattr(session, "profile", None)
    if profile is not None:
        for fp in getattr(profile, "structural_fingerprints", None) or []:
            col = fp.column if hasattr(fp, "column") else fp.get("column")
            if col:
                fp_by_col[col] = fp.to_dict() if hasattr(fp, "to_dict") else fp

    if not fp_by_col:
        try:
            fp_store = FingerprintStore.from_env(store.backend, store.bucket)
            for row in fp_store.list_table(table):
                col = row.get("column")
                if col:
                    fp_by_col[col] = row
        except Exception as exc:
            log.debug("fingerprint store read failed: %s", exc)

    contract = store.get_active(table) or {}
    contract_uuid = contract.get("id") or contract.get("contract_uuid")
    vector_store = get_vector_store()

    golden_batch: list[GoldenColumn] = []
    for col, prop in _collect_promotion_candidates(session, contract):
        golden = golden_from_contract_prop(
            table,
            col,
            prop,
            fingerprint=fp_by_col.get(col),
            contract_uuid=contract_uuid,
            approved_by=approved_by,
        )
        if golden is None:
            log.warning(
                "skip golden promotion for %s.%s: no structural fingerprint",
                table, col,
            )
            continue
        golden_batch.append(golden)
        log.info("promoted golden column %s.%s (kind-agnostic)", table, col)

    if golden_batch:
        vector_store.upsert_many(golden_batch)

    return len(golden_batch)
