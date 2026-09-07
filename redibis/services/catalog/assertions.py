"""Catalog assertion model — pure projection of ODCS into governed facts.

No I/O, no httpx, no backend imports. The assertion builder is a projection of
the active contract, not a second source of truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from redibis.services.catalog.mapping import (
    column_catalog_tag_names,
    iter_columns,
    redibis_glossary_name,
    split_physical_name,
    table_semantics,
)

logger = logging.getLogger(__name__)


class Authority(str, Enum):
    REDIBIS = "redibis"
    HUMAN = "human"
    EXTERNAL = "external"  # OM connector or a third tool
    PROPAGATED = "propagated"  # lineage-derived
    UNKNOWN = "unknown"


class Facet(str, Enum):
    TABLE_DESCRIPTION = "table_description"
    COLUMN_DESCRIPTION = "column_description"
    COLUMN_DISPLAY_NAME = "column_display_name"
    PII_TAG = "pii_tag"
    POLICY_TAG = "policy_tag"
    ENTITY_TAG = "entity_tag"
    GLOSSARY_LINK = "glossary_link"
    QUALITY_TEST = "quality_test"


class CoverageStatus(str, Enum):
    EVALUATED = "evaluated"  # only status that permits deletion
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class AssertionKey:
    """Stable identity of one governed fact. Hashable; the join key everywhere."""

    asset_fqn: str
    column_path: str  # "" for table-level; dotted for nested/struct columns
    facet: Facet
    value_key: str  # tag FQN / term FQN / rule_id; "" for singleton facets


@dataclass
class Assertion:
    key: AssertionKey
    value: Any  # str for descriptions; dict/str for tags/tests
    authority: Authority
    confidence: float = 1.0
    scan_id: str = ""
    rule_id: str = ""
    evidence_ref: str = ""  # pointer into run outputs; NEVER raw sample values
    observed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass(frozen=True)
class CoverageRecord:
    scan_id: str
    asset_fqn: str
    column_path: str
    facet: Facet
    status: CoverageStatus
    reason: str = ""


@dataclass
class LedgerEntry:
    """What Redibis previously published to one backend. The ownership record."""

    backend: str
    key: AssertionKey
    value_hash: str  # sha256 of canonical JSON of the published value
    scan_id: str
    published_at: datetime
    state: str = "confirmed"  # "confirmed" | "suggested"
    negative_streak: int = 0  # consecutive EVALUATED scans that did not assert


@dataclass
class Suppression:
    """A durable negative human assertion. Blocks republication."""

    key: AssertionKey
    actor: str
    reason: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    source: str = "om_change_event"  # | "cli" | "ui"


# ── Serialization helpers ────────────────────────────────────────────────────


def value_hash(value: Any) -> str:
    """SHA-256 of canonical JSON (sort_keys, default=str)."""
    payload = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assertion_key_to_dict(key: AssertionKey) -> dict:
    return {
        "asset_fqn": key.asset_fqn,
        "column_path": key.column_path,
        "facet": key.facet.value if isinstance(key.facet, Facet) else str(key.facet),
        "value_key": key.value_key,
    }


def assertion_key_from_dict(d: dict) -> AssertionKey:
    return AssertionKey(
        asset_fqn=str(d.get("asset_fqn") or ""),
        column_path=str(d.get("column_path") or ""),
        facet=Facet(d["facet"]),
        value_key=str(d.get("value_key") or ""),
    )


def coverage_record_to_dict(rec: CoverageRecord) -> dict:
    return {
        "scan_id": rec.scan_id,
        "asset_fqn": rec.asset_fqn,
        "column_path": rec.column_path,
        "facet": rec.facet.value if isinstance(rec.facet, Facet) else str(rec.facet),
        "status": rec.status.value if isinstance(rec.status, CoverageStatus) else str(rec.status),
        "reason": rec.reason or "",
    }


def coverage_record_from_dict(d: dict) -> CoverageRecord:
    return CoverageRecord(
        scan_id=str(d.get("scan_id") or ""),
        asset_fqn=str(d.get("asset_fqn") or ""),
        column_path=str(d.get("column_path") or ""),
        facet=Facet(d["facet"]),
        status=CoverageStatus(d.get("status") or CoverageStatus.EVALUATED.value),
        reason=str(d.get("reason") or ""),
    )


def rebind_coverage_asset_fqn(
    records: list[CoverageRecord],
    asset_fqn: str,
) -> list[CoverageRecord]:
    """Return coverage records with ``asset_fqn`` set to the resolved catalog FQN."""
    from dataclasses import replace

    out: list[CoverageRecord] = []
    for rec in records:
        if rec.asset_fqn == asset_fqn:
            out.append(rec)
        else:
            out.append(replace(rec, asset_fqn=asset_fqn))
    return out


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _dt_from_iso(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ledger_entry_to_dict(entry: LedgerEntry) -> dict:
    return {
        "backend": entry.backend,
        "key": assertion_key_to_dict(entry.key),
        "value_hash": entry.value_hash,
        "scan_id": entry.scan_id,
        "published_at": _dt_to_iso(entry.published_at),
        "state": entry.state,
        "negative_streak": int(entry.negative_streak),
    }


def ledger_entry_from_dict(d: dict) -> LedgerEntry:
    return LedgerEntry(
        backend=str(d.get("backend") or ""),
        key=assertion_key_from_dict(d.get("key") or {}),
        value_hash=str(d.get("value_hash") or ""),
        scan_id=str(d.get("scan_id") or ""),
        published_at=_dt_from_iso(d.get("published_at")),
        state=str(d.get("state") or "confirmed"),
        negative_streak=int(d.get("negative_streak") or 0),
    )


def suppression_to_dict(sup: Suppression) -> dict:
    return {
        "key": assertion_key_to_dict(sup.key),
        "actor": sup.actor,
        "reason": sup.reason,
        "created_at": _dt_to_iso(sup.created_at),
        "expires_at": _dt_to_iso(sup.expires_at) if sup.expires_at else None,
        "source": sup.source,
    }


def suppression_from_dict(d: dict) -> Suppression:
    expires_raw = d.get("expires_at")
    return Suppression(
        key=assertion_key_from_dict(d.get("key") or {}),
        actor=str(d.get("actor") or ""),
        reason=str(d.get("reason") or ""),
        created_at=_dt_from_iso(d.get("created_at")),
        expires_at=_dt_from_iso(expires_raw) if expires_raw else None,
        source=str(d.get("source") or "om_change_event"),
    )


# ── Assertion builder ────────────────────────────────────────────────────────


def _classification_confidence(
    classification_results: Optional[list],
    column: str,
) -> Optional[float]:
    """Best available confidence for a column from classification results."""
    if not classification_results:
        return None
    best: Optional[float] = None
    for result in classification_results:
        if isinstance(result, dict):
            col = str(result.get("column") or "")
            if col != column:
                continue
            raw = result.get("confidence")
        else:
            col = getattr(result, "column", "") or ""
            if col != column:
                continue
            raw = getattr(result, "confidence", None)
        try:
            conf = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            conf = None
        if conf is None:
            continue
        if best is None or conf > best:
            best = conf
    return best


def _tag_facet(tag_name: str) -> Facet:
    if tag_name.startswith("PII."):
        return Facet.PII_TAG
    if tag_name.startswith("RedibisPolicy."):
        return Facet.POLICY_TAG
    return Facet.ENTITY_TAG


def _coverage(
    *,
    scan_id: str,
    asset_fqn: str,
    column_path: str,
    facet: Facet,
    status: CoverageStatus = CoverageStatus.EVALUATED,
    reason: str = "",
) -> CoverageRecord:
    return CoverageRecord(
        scan_id=scan_id,
        asset_fqn=asset_fqn,
        column_path=column_path,
        facet=facet,
        status=status,
        reason=reason,
    )


def build_assertions_from_contract(
    contract: dict,
    table: str,
    *,
    asset_fqn: str,
    scan_id: str = "",
    classification_results: Optional[list] = None,
    include_tags: bool = True,
    include_glossary: bool = True,
    publish_threshold: float = 0.0,
    column_telemetry: Optional[dict] = None,
    metadata_store: Any = None,
    scan_coverage: Optional[list[CoverageRecord]] = None,
) -> tuple[list[Assertion], list[CoverageRecord]]:
    """Project an ODCS contract into assertions + coverage for one asset.

    When ``scan_coverage`` is provided it is used verbatim (no coverage of our
    own). When ``None``, coverage is derived from the contract column list and a
    warning is logged — deletions are then gated on contract membership only.

    Tag confidence precedence: classification result → telemetry confidence
    (from ``metadata_store`` / ``column_telemetry``) → ``1.0``. When the value
    is defaulted, ``evidence_ref="contract-boolean"``. Never embeds sample
    values in ``evidence_ref``.

    ``metadata_store`` is an optional ``ContractMetadataStore`` injected by the
    caller; this module performs no I/O of its own.
    """
    assertions: list[Assertion] = []
    now = datetime.now(timezone.utc)
    database, _table_name = split_physical_name(table)
    sem = table_semantics(contract, table)

    telemetry = column_telemetry
    if telemetry is None and metadata_store is not None:
        try:
            telemetry = metadata_store.get_column_telemetry(table) or {}
        except Exception:  # noqa: BLE001 — store is optional; stay pure on failure
            telemetry = {}
    telemetry = telemetry or {}

    use_scan_coverage = scan_coverage is not None
    coverage: list[CoverageRecord] = list(scan_coverage) if use_scan_coverage else []
    if not use_scan_coverage:
        logger.warning(
            "coverage derived from contract, not from a scan — deletions are "
            "gated on contract membership only (table=%s)",
            table,
        )

    def _cov(column_path: str, facet: Facet) -> CoverageRecord:
        return _coverage(
            scan_id=scan_id,
            asset_fqn=asset_fqn,
            column_path=column_path,
            facet=facet,
        )

    # Table-level description
    if not use_scan_coverage:
        coverage.append(_cov("", Facet.TABLE_DESCRIPTION))
    table_desc = (sem.description or "").strip()
    if table_desc and 1.0 >= publish_threshold:
        assertions.append(
            Assertion(
                key=AssertionKey(
                    asset_fqn=asset_fqn,
                    column_path="",
                    facet=Facet.TABLE_DESCRIPTION,
                    value_key="",
                ),
                value=table_desc,
                authority=Authority.REDIBIS,
                confidence=1.0,
                scan_id=scan_id,
                observed_at=now,
            )
        )

    tag_facets = (Facet.PII_TAG, Facet.ENTITY_TAG, Facet.POLICY_TAG)

    for col in iter_columns(
        contract, table, classification_results=classification_results
    ):
        col_path = col.name
        class_conf = _classification_confidence(classification_results, col.name)
        tel = telemetry.get(col_path) if isinstance(telemetry.get(col_path), dict) else {}
        tel_conf_raw = tel.get("confidence") if isinstance(tel, dict) else None
        try:
            tel_conf = float(tel_conf_raw) if tel_conf_raw is not None else None
        except (TypeError, ValueError):
            tel_conf = None

        if not use_scan_coverage:
            coverage.append(_cov(col_path, Facet.COLUMN_DESCRIPTION))
            coverage.append(_cov(col_path, Facet.COLUMN_DISPLAY_NAME))

        col_desc = (col.description or "").strip()
        if col_desc and 1.0 >= publish_threshold:
            assertions.append(
                Assertion(
                    key=AssertionKey(
                        asset_fqn=asset_fqn,
                        column_path=col_path,
                        facet=Facet.COLUMN_DESCRIPTION,
                        value_key="",
                    ),
                    value=col_desc,
                    authority=Authority.REDIBIS,
                    confidence=1.0,
                    scan_id=scan_id,
                    observed_at=now,
                )
            )

        display = (col.business_name or "").strip()
        if display and 1.0 >= publish_threshold:
            assertions.append(
                Assertion(
                    key=AssertionKey(
                        asset_fqn=asset_fqn,
                        column_path=col_path,
                        facet=Facet.COLUMN_DISPLAY_NAME,
                        value_key="",
                    ),
                    value=display,
                    authority=Authority.REDIBIS,
                    confidence=1.0,
                    scan_id=scan_id,
                    observed_at=now,
                )
            )

        if not use_scan_coverage:
            for facet in tag_facets:
                coverage.append(_cov(col_path, facet))

        tag_names = column_catalog_tag_names(col, include_tags=include_tags)
        conf_defaulted = False
        if class_conf is not None:
            tag_conf = float(class_conf)
        elif tel_conf is not None:
            tag_conf = float(tel_conf)
        else:
            tag_conf = 1.0
            conf_defaulted = True
        tag_evidence = "contract-boolean" if conf_defaulted else ""
        if tag_conf >= publish_threshold:
            for tag_name in tag_names:
                facet = _tag_facet(tag_name)
                assertions.append(
                    Assertion(
                        key=AssertionKey(
                            asset_fqn=asset_fqn,
                            column_path=col_path,
                            facet=facet,
                            value_key=tag_name,
                        ),
                        value=tag_name,
                        authority=Authority.REDIBIS,
                        confidence=tag_conf,
                        scan_id=scan_id,
                        evidence_ref=tag_evidence,
                        observed_at=now,
                    )
                )

        if not use_scan_coverage:
            coverage.append(_cov(col_path, Facet.GLOSSARY_LINK))
        if include_glossary and display and 1.0 >= publish_threshold:
            term_fqn = f"{redibis_glossary_name(database)}.{col.name}"
            assertions.append(
                Assertion(
                    key=AssertionKey(
                        asset_fqn=asset_fqn,
                        column_path=col_path,
                        facet=Facet.GLOSSARY_LINK,
                        value_key=term_fqn,
                    ),
                    value=display,
                    authority=Authority.REDIBIS,
                    confidence=1.0,
                    scan_id=scan_id,
                    observed_at=now,
                )
            )

    return assertions, coverage
