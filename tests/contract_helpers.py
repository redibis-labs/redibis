"""Shared helpers for contract structure assertions (privacy block + metadata)."""

from redibis.contracts.privacy import (
    col_entity_type,
    col_masking_policy,
    col_pii_engine,
    col_privacy_classification,
)

__all__ = [
    "col_pii_engine",
    "col_entity_type",
    "col_privacy_classification",
    "col_masking_default",
]


def col_masking_default(prop: dict) -> str:
    mp = col_masking_policy(prop)
    return mp.get("default_strategy") or mp.get("default") or ""
