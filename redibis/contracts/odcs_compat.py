"""
redibis.contracts.odcs_compat — strip redibis-only fields before ODCS Pydantic validation.

``OpenDataContractStandard`` forbids unknown top-level keys (``extra_forbidden``).
Redibis keeps identity/routing/telemetry alongside the spec; this module removes
those extensions before handing a contract to datacontract-cli or Pydantic.
"""

from __future__ import annotations

import copy
from typing import Any

# Top-level keys redibis adds that are NOT part of the ODCS schema.
REDIBIS_ONLY_TOP_LEVEL: frozenset[str] = frozenset({
    "database_name",
    "table_name",
    "contract_uuid",
    "pii_summary",
    "last_updated",
    "last_updated_by_workflow",
    "provenance",
    "_scan_metadata",
})

# Column-level extensions not in ODCS (canonical ``privacy`` + legacy keys).
REDIBIS_COLUMN_EXTENSIONS: tuple[str, ...] = ("privacy", "pii", "maskingPolicy")


def prepare_for_odcs_pydantic(contract: dict) -> dict:
    """Return a deep copy safe to pass to ``OpenDataContractStandard(**...)``."""
    clean = {k: v for k, v in contract.items() if k not in REDIBIS_ONLY_TOP_LEVEL}
    if "schema" not in clean:
        return clean
    clean = copy.deepcopy(clean)
    for schema_obj in clean.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            for key in REDIBIS_COLUMN_EXTENSIONS:
                prop.pop(key, None)
    return clean


def build_odcs_model(contract: dict) -> Any:
    """Parse a redibis contract dict into ``OpenDataContractStandard``."""
    from open_data_contract_standard.model import OpenDataContractStandard
    return OpenDataContractStandard(**prepare_for_odcs_pydantic(contract))
