"""Column fingerprint snapshots for steward decision drift detection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Optional

from redibis.memory.fingerprint import normalize_column_name
from redibis.memory.store import canonical_fingerprint_key


@dataclass(frozen=True)
class ColumnFingerprintSnapshot:
    column: str
    name_normalized: str
    logical_type: str
    physical_type: str
    format_signature: str
    fingerprint_key: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ColumnFingerprintSnapshot":
        column = str(data.get("column") or "")
        name_norm = str(data.get("name_normalized") or normalize_column_name(column))
        logical = str(data.get("logical_type") or "string")
        physical = str(data.get("physical_type") or logical)
        fmt = str(data.get("format_signature") or "unknown")
        fp_key = str(data.get("fingerprint_key") or canonical_fingerprint_key(name_norm, logical, fmt))
        return cls(
            column=column,
            name_normalized=name_norm,
            logical_type=logical,
            physical_type=physical,
            format_signature=fmt,
            fingerprint_key=fp_key,
        )


def _format_signature_from_profile(profile: dict) -> str:
    if not isinstance(profile, dict):
        return "unknown"
    masks = profile.get("format_masks")
    if isinstance(masks, dict):
        top = masks.get("top_masks") or masks.get("masks") or []
        if isinstance(top, list) and top:
            first = top[0]
            if isinstance(first, dict):
                mask = first.get("mask") or first.get("pattern")
                if mask:
                    return str(mask)
            elif isinstance(first, str):
                return first
    charset = profile.get("charset")
    if isinstance(charset, dict):
        dominant = charset.get("dominant_script")
        if dominant:
            return f"script:{dominant}"
    inferred = profile.get("inferred_class")
    if inferred:
        return f"class:{inferred}"
    return "unknown"


def _build_snapshot(
    column: str,
    *,
    logical_type: str,
    physical_type: str,
    profile: Optional[dict] = None,
) -> ColumnFingerprintSnapshot:
    logical = str(logical_type or "string")
    physical = str(physical_type or logical)
    fmt = _format_signature_from_profile(profile or {})
    name_norm = normalize_column_name(column)
    return ColumnFingerprintSnapshot(
        column=column,
        name_normalized=name_norm,
        logical_type=logical,
        physical_type=physical,
        format_signature=fmt,
        fingerprint_key=canonical_fingerprint_key(name_norm, logical, fmt),
    )


def fingerprint_from_evidence_column(column: str, col_block: dict) -> ColumnFingerprintSnapshot:
    """Derive a drift baseline from one ``evidence_bundle.columns`` entry."""
    block = col_block or {}
    profile = block.get("profile") if isinstance(block.get("profile"), dict) else {}
    return _build_snapshot(
        column,
        logical_type=str(block.get("logical_type") or profile.get("logical_type") or "string"),
        physical_type=str(block.get("physical_type") or profile.get("physical_type") or "string"),
        profile=profile,
    )


def fingerprint_from_contract_prop(column: str, prop: dict) -> ColumnFingerprintSnapshot:
    """Derive a drift baseline from an ODCS column property."""
    p = prop or {}
    return _build_snapshot(
        column,
        logical_type=str(p.get("logicalType") or p.get("logical_type") or "string"),
        physical_type=str(p.get("physicalType") or p.get("physical_type") or "string"),
        profile={},
    )


def fingerprint_metadata_from_prop(column: str, prop: dict) -> dict[str, str]:
    """Keyword args for ``ContractStore.set_pii_decision`` fingerprint fields."""
    fp = fingerprint_from_contract_prop(column, prop)
    return {
        "fingerprint_key": fp.fingerprint_key,
        "name_normalized": fp.name_normalized,
        "logical_type": fp.logical_type,
        "physical_type": fp.physical_type,
        "format_signature": fp.format_signature,
    }


def fingerprint_metadata_from_evidence(column: str, col_block: dict) -> dict[str, str]:
    """Keyword args for ``ContractStore.set_pii_decision`` from a bundle column."""
    fp = fingerprint_from_evidence_column(column, col_block)
    return {
        "fingerprint_key": fp.fingerprint_key,
        "name_normalized": fp.name_normalized,
        "logical_type": fp.logical_type,
        "physical_type": fp.physical_type,
        "format_signature": fp.format_signature,
    }


def evidence_digest(col_block: dict) -> str:
    """Privacy-safe hash of engine evidence for audit (no sample values)."""
    block = col_block or {}
    payload = {
        "pii_verdict": block.get("pii_verdict"),
        "rules_fired": block.get("rules_fired"),
        "negative_signals_fired": block.get("negative_signals_fired"),
        "validator_results": block.get("validator_results"),
        "coverage": block.get("coverage"),
    }
    pii_ev = block.get("pii_evidence")
    if isinstance(pii_ev, dict):
        payload["pii_evidence_keys"] = sorted(pii_ev.keys())
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
