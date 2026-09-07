"""
redibis.memory.writer — record human review decisions into the memory store.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from redibis.config import MemoryConfig
from redibis.memory.consent import SamplingConsentStore
from redibis.memory.decision import ReviewDecision
from redibis.memory.embedding import EmbeddingProvider, get_embedding_provider
from redibis.memory.fingerprint import (
    ColumnFingerprint,
    FingerprintField,
    _cardinality_class,
    compute_format_signature,
    normalize_column_name,
)
from redibis.memory.rationale import apply_rationale
from redibis.memory.redaction import redact_samples
from redibis.memory.store import (
    MemoryStore,
    canonical_fingerprint_key,
    get_memory_store,
)

from redibis.memory.async_writer import (
    record_memory_write_failure,
    record_memory_write_success,
    submit_memory_task,
)

log = logging.getLogger(__name__)


def _dispatch_memory_writes(fn, *, async_writes: bool) -> None:
    if async_writes:
        submit_memory_task(fn)
    else:
        try:
            fn()
        except Exception as exc:
            record_memory_write_failure(reason=str(exc))
            log.warning("memory write failed: %s", exc)


def _catalog_ndv_from_prop(prop: dict) -> tuple[Optional[int], Optional[int]]:
    """Read NDV / row_count from column metadata blocks when catalog stats were merged."""
    ndv: Optional[int] = None
    row_count: Optional[int] = None
    for container_key in ("customProperties", "catalog", "metadata", "stats"):
        container = prop.get(container_key) or {}
        if not isinstance(container, dict):
            continue
        raw_ndv = (
            container.get("ndv")
            or container.get("distinct_count")
            or container.get("numDistinctValues")
        )
        if raw_ndv is not None:
            try:
                ndv = int(raw_ndv)
            except (TypeError, ValueError):
                pass
        raw_rows = container.get("row_count") or container.get("numRows")
        if raw_rows is not None:
            try:
                row_count = int(raw_rows)
            except (TypeError, ValueError):
                pass
    return ndv, row_count


def fingerprint_from_column_prop(
    table: str,
    column: str,
    prop: dict,
    *,
    domain: str = "",
    sample_values: Optional[list[Any]] = None,
) -> ColumnFingerprint:
    """Build a minimal fingerprint from an ODCS column property dict."""
    samples = [str(v) for v in (sample_values or []) if v is not None]
    sig = _infer_format_signature(prop, samples)
    shapes = redact_samples(samples, table=table, column=column, approved=False)
    logical = prop.get("logicalType") or "string"
    physical = prop.get("physicalType") or logical
    ndv, row_count = _catalog_ndv_from_prop(prop)
    if ndv is not None:
        card = _cardinality_class(
            unique_ratio=(ndv / row_count) if row_count else None,
            unique_count=ndv,
            row_count=row_count,
        )
        card_field = FingerprintField(card, "catalog", "approximate")
    else:
        card_field = FingerprintField("unknown", "contract", "approximate")
    return ColumnFingerprint(
        table=table,
        column=column,
        domain=domain,
        name_normalized=normalize_column_name(column),
        logical_type=FingerprintField(logical, "contract", "stable"),
        physical_type=FingerprintField(physical, "contract", "stable"),
        format_signature=FingerprintField(sig, "sample" if samples else "contract", "stable"),
        cardinality_class=card_field,
        nullable=FingerprintField(True, "contract", "stable"),
        redacted_samples=shapes,
        entity_type=_entity_from_prop(prop),
    )


def _entity_from_prop(prop: dict) -> Optional[str]:
    privacy = prop.get("privacy") or {}
    if isinstance(privacy, dict):
        engine = privacy.get("classification_engine") or {}
        if isinstance(engine, dict) and engine.get("entity_type"):
            return str(engine["entity_type"])
    return None


_ENTITY_FORMAT_HINTS: dict[str, str] = {
    "EMAIL_ADDRESS": "email_address",
    "EMAIL": "email_address",
    "EG_NATIONAL_ID": "national_id_egypt_strict",
    "NATIONAL_ID": "national_id_egypt_strict",
    "PHONE_NUMBER": "phone_number",
    "IBAN_CODE": "iban",
}


def _infer_format_signature(prop: dict, samples: list[str]) -> str:
    sig, _ = compute_format_signature(samples)
    if sig not in ("empty", "mixed"):
        return sig
    entity = (_entity_from_prop(prop) or "").upper()
    if entity in _ENTITY_FORMAT_HINTS:
        return _ENTITY_FORMAT_HINTS[entity]
    col = normalize_column_name(str(prop.get("name") or ""))
    if "email" in col:
        return "email_address"
    if "national_id" in col or col.endswith("_nid"):
        return "national_id_egypt_strict"
    return sig


def _classification_from_prop(prop: dict) -> Optional[str]:
    val = prop.get("classification")
    return str(val) if val else None


def _masking_from_prop(prop: dict) -> Optional[dict]:
    privacy = prop.get("privacy") or {}
    if isinstance(privacy, dict) and privacy.get("masking_policy"):
        return dict(privacy["masking_policy"])
    legacy = prop.get("maskingPolicy")
    return dict(legacy) if isinstance(legacy, dict) else None


def record_review(
    *,
    memory_config: MemoryConfig,
    fingerprint: ColumnFingerprint,
    decision: ReviewDecision,
    consent_store: Optional[SamplingConsentStore] = None,
    sample_values: Optional[list[Any]] = None,
    memory_store: Optional[MemoryStore] = None,
    embedding: Optional[EmbeddingProvider] = None,
) -> Optional[dict]:
    """
    Embed + upsert a review decision when ``memory.enabled``.

    Returns a small result dict, or ``None`` when memory is disabled.
    """
    if not memory_config.enabled:
        return None

    try:
        store = memory_store or get_memory_store(memory_config, embedding=embedding)
        if store is None:
            return None

        if sample_values:
            fingerprint.redacted_samples = redact_samples(
                sample_values,
                table=fingerprint.table,
                column=fingerprint.column,
                consent=consent_store,
            )

        decision.fingerprint_key = canonical_fingerprint_key(
            fingerprint.name_normalized,
            str(fingerprint.logical_type.value),
            str(fingerprint.format_signature.value),
        )
        decision = apply_rationale(decision, fingerprint)

        card = fingerprint.to_column_card()
        embedder = embedding or get_embedding_provider(memory_config)
        vector = embedder.embed(card)

        fp_dict = {
            "table": fingerprint.table,
            "column": fingerprint.column,
            "name_normalized": fingerprint.name_normalized,
            "logical_type": fingerprint.logical_type.value,
            "format_signature": fingerprint.format_signature.value,
            "domain": fingerprint.domain,
            "entity_type": fingerprint.entity_type,
            "redacted_samples": fingerprint.redacted_samples,
        }

        result = store.upsert(
            canonical_key=decision.fingerprint_key,
            fingerprint=fp_dict,
            embedding=vector,
            decision=decision.to_dict(),
            domain=fingerprint.domain,
            logical_type=str(fingerprint.logical_type.value),
            format_signature=str(fingerprint.format_signature.value),
            entity_type=fingerprint.entity_type,
            column_card=card,
        )
        record_memory_write_success()
        return {
            "canonical_key": result.canonical_key,
            "occurrence_count": result.occurrence_count,
            "created": result.created,
        }
    except Exception as exc:
        record_memory_write_failure(reason=str(exc))
        log.warning("memory write skipped for %s.%s: %s", fingerprint.table, fingerprint.column, exc)
        return None


def record_approved_merge(
    session,
    store,
    *,
    memory_config: Optional[MemoryConfig],
    merged_version: Optional[str],
) -> None:
    """Hook for ``merge_approved`` — one memory write per approved basket item."""
    cfg = memory_config or MemoryConfig()
    if not cfg.enabled or not merged_version:
        return

    consent = SamplingConsentStore(store.backend, store.bucket)
    fp_by_col = _fingerprints_for_session(session)
    domain = cfg.domain or ""
    memory_store = getattr(store, "_memory_store", None)

    def _write_all() -> None:
        for item in session.approved.items:
            if item.status != "merged":
                continue
            fp = fp_by_col.get(item.column) or fingerprint_from_column_prop(
                session.table_name,
                item.column,
                item.payload,
                domain=domain,
            )
            decision = _decision_from_approved_item(
                item,
                fingerprint_key="",
                table=session.table_name,
                contract_version=merged_version,
                reviewer=f"session:{session.session_id}",
            )
            record_review(
                memory_config=cfg,
                fingerprint=fp,
                decision=decision,
                consent_store=consent,
                memory_store=memory_store,
            )

    _dispatch_memory_writes(_write_all, async_writes=cfg.async_writes)


def record_contract_pii_decision(
    store,
    table: str,
    column: str,
    *,
    status: str,
    payload: Optional[dict],
    upsert_result,
    memory_config: Optional[MemoryConfig],
    decided_by: str = "",
    run_id: str = "",
) -> None:
    cfg = memory_config or getattr(store, "memory_config", None) or MemoryConfig()
    if not cfg.enabled:
        return

    active = store.get_active(table) or {}
    prop = _column_prop(active, column) or {"name": column, "logicalType": "string"}
    if payload:
        prop = {**prop, **payload}

    fp = fingerprint_from_column_prop(table, column, prop, domain=cfg.domain or "")
    decision = ReviewDecision(
        fingerprint_key="",
        table=table,
        column=column,
        pii_verdict=status,
        masking_strategy=_masking_from_prop(prop),
        classification=_classification_from_prop(prop),
        reviewer=decided_by,
        contract_version=getattr(upsert_result, "version_after", "") or "",
        provenance={"workflow": "pii_decision", "run_id": run_id},
    )

    def _write() -> None:
        record_review(
            memory_config=cfg,
            fingerprint=fp,
            decision=decision,
            consent_store=SamplingConsentStore(store.backend, store.bucket),
            memory_store=getattr(store, "_memory_store", None),
        )

    _dispatch_memory_writes(_write, async_writes=cfg.async_writes)


def record_contract_definitions_patch(
    store,
    table: str,
    column_patches: Optional[dict[str, dict]],
    *,
    upsert_result,
    memory_config: Optional[MemoryConfig],
    decided_by: str = "",
    run_id: str = "",
) -> None:
    cfg = memory_config or getattr(store, "memory_config", None) or MemoryConfig()
    if not cfg.enabled or not column_patches:
        return

    active = store.get_active(table) or {}
    consent = SamplingConsentStore(store.backend, store.bucket)
    memory_store = getattr(store, "_memory_store", None)
    writes: list[tuple[ColumnFingerprint, ReviewDecision]] = []

    for column, patch in column_patches.items():
        prop = _column_prop(active, column) or {"name": column}
        merged = {**prop, **patch}
        fp = fingerprint_from_column_prop(
            table, column, merged, domain=cfg.domain or "",
        )
        biz = patch.get("business")
        biz_text = None
        if isinstance(biz, dict):
            biz_text = biz.get("description") or biz.get("definition")
        elif isinstance(biz, str):
            biz_text = biz
        decision = ReviewDecision(
            fingerprint_key="",
            table=table,
            column=column,
            business_definition=biz_text or patch.get("description") or patch.get("businessName"),
            classification=merged.get("classification"),
            reviewer=decided_by,
            contract_version=getattr(upsert_result, "version_after", "") or "",
            provenance={"workflow": "definitions_patch", "run_id": run_id},
        )
        writes.append((fp, decision))

    def _write_all() -> None:
        for fp, decision in writes:
            record_review(
                memory_config=cfg,
                fingerprint=fp,
                decision=decision,
                consent_store=consent,
                memory_store=memory_store,
            )

    _dispatch_memory_writes(_write_all, async_writes=cfg.async_writes)


def _fingerprints_for_session(session) -> dict[str, ColumnFingerprint]:
    profile = getattr(session, "profiler", None)
    if profile and getattr(profile, "fingerprints", None):
        return {fp.column: fp for fp in profile.fingerprints}
    return {}


def _column_prop(contract: dict, column: str) -> Optional[dict]:
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict) and prop.get("name") == column:
                return prop
    return None


def _decision_from_approved_item(
    item,
    *,
    fingerprint_key: str,
    table: str,
    contract_version: str,
    reviewer: str,
) -> ReviewDecision:
    decision = ReviewDecision(
        fingerprint_key=fingerprint_key,
        table=table,
        column=item.column,
        reviewer=reviewer,
        contract_version=contract_version,
        provenance={
            "workflow": "approved_merge",
            "kind": item.kind,
            "source": item.source,
            "source_run_id": item.source_run_id,
        },
    )
    if item.kind == "pii":
        decision.pii_verdict = "pii"
        decision.classification = item.payload.get("classification")
        decision.masking_strategy = _masking_from_prop(item.payload)
    elif item.kind == "quality":
        decision.quality_rules = [dict(item.payload)]
    return decision
