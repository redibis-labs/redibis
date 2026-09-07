"""
redibis.contracts.retention
============================
Table-level data retention (TTL) for ODCS contracts.

Why this exists
---------------
CDR / location-bearing telecom data is usually under a legal cap on how long
the raw record may be kept (e.g. 90 days). The contract is the right place to
declare that limit so downstream platforms can enforce it.

Storage shape (ODCS-native)
---------------------------
Retention lives under the top-level ODCS ``slaProperties`` list as a single
entry whose ``property`` is ``retention``::

    slaProperties:
      - property: retention
        value: 90
        unit: d
        driver: regulatory        # optional: why this limit exists
        element: event_timestamp  # optional: the column the TTL is measured from

Default = "NA"
--------------
Per policy, retention is *never blank*: when no retention entry is present,
``get_retention()`` reports ``value="NA"`` (and ``unit="NA"``) rather than
``None``. ``set_retention(value="NA")`` writes an explicit NA entry so the
"not set" state is visible in the contract itself.

Single-writer rule
-------------------
This module only *builds*/*reads*/*edits dicts*. Persisting a change to the
contracts bucket still goes through ``ContractStore.upsert()`` (the one and
only writer). ``ContractStore.set_retention()`` wires the two together.
"""

from __future__ import annotations

from typing import Any, Optional

RETENTION_PROPERTY = "retention"
NA = "NA"

#: Informational set of common ODCS duration units. Not enforced — any string
#: is accepted so teams can use whatever their platform understands.
COMMON_UNITS: tuple[str, ...] = ("d", "w", "m", "y", "h", "min", "s")


def _sla_list(contract: dict) -> list[dict]:
    """Return the contract's slaProperties list (never None)."""
    sla = contract.get("slaProperties")
    return sla if isinstance(sla, list) else []


def get_retention(contract: dict) -> dict:
    """
    Read the retention SLA entry from a contract.

    Returns a normalized dict that ALWAYS has ``value`` and ``unit`` keys.
    When no retention entry exists, both default to ``"NA"`` (never None).

    Returns
    -------
    {
        "value":   90 | "NA",
        "unit":    "d" | "NA",
        "driver":  "regulatory" | None,
        "element": "event_timestamp" | None,
        "set":     True | False,        # whether the contract actually declares it
    }
    """
    for entry in _sla_list(contract):
        if isinstance(entry, dict) and entry.get("property") == RETENTION_PROPERTY:
            return {
                "value":   entry.get("value", NA),
                "unit":    entry.get("unit", NA),
                "driver":  entry.get("driver"),
                "element": entry.get("element"),
                "set":     True,
            }
    return {"value": NA, "unit": NA, "driver": None, "element": None, "set": False}


def _normalize_value(value: Any) -> Any:
    """Coerce a retention value: ints stay ints, NA-ish strings become 'NA'."""
    if value is None:
        return NA
    if isinstance(value, str):
        s = value.strip()
        if s == "" or s.upper() == NA:
            return NA
        if s.isdigit():
            return int(s)
        return s
    return value


def set_retention(
    contract: dict,
    value: Any = NA,
    *,
    unit: str = NA,
    driver: Optional[str] = None,
    element: Optional[str] = None,
) -> dict:
    """
    Set (or replace) the retention entry on a contract dict, in place.

    Usable directly in code / notebooks for a manual edit. Persisting the
    result to the contracts bucket must still go through
    ``ContractStore.upsert()`` (see ``ContractStore.set_retention``).

    Args:
        value   : retention duration (int) or ``"NA"`` to mark it explicitly unset.
        unit    : duration unit (``d``, ``m``, ``y`` …). Forced to ``"NA"`` when
                  ``value`` is ``"NA"``.
        driver  : optional reason (e.g. ``"regulatory"``, ``"legal_hold"``).
        element : optional column the TTL is measured from (e.g. ``"created_at"``).

    Returns:
        The same contract dict (mutated), for chaining.
    """
    norm_value = _normalize_value(value)
    norm_unit = NA if norm_value == NA else (unit or NA)

    entry: dict[str, Any] = {
        "property": RETENTION_PROPERTY,
        "value": norm_value,
        "unit": norm_unit,
    }
    if driver:
        entry["driver"] = driver
    if element:
        entry["element"] = element

    sla = contract.get("slaProperties")
    if not isinstance(sla, list):
        sla = []
    # Replace any existing retention entry; keep all other SLA properties intact.
    sla = [e for e in sla
           if not (isinstance(e, dict) and e.get("property") == RETENTION_PROPERTY)]
    sla.append(entry)
    contract["slaProperties"] = sla
    return contract


def clear_retention(contract: dict) -> dict:
    """Remove the retention entry entirely (drops slaProperties if it empties)."""
    sla = [e for e in _sla_list(contract)
           if not (isinstance(e, dict) and e.get("property") == RETENTION_PROPERTY)]
    if sla:
        contract["slaProperties"] = sla
    else:
        contract.pop("slaProperties", None)
    return contract


def build_retention_partial(
    table: str,
    value: Any = NA,
    *,
    unit: str = NA,
    driver: Optional[str] = None,
    element: Optional[str] = None,
    physical_name: Optional[str] = None,
) -> dict:
    """
    Build a minimal ODCS partial that carries only the retention SLA entry.

    The partial is mergeable via ``ContractStore.upsert()`` — slaProperties is
    a top-level, last-writer-wins field, so this replaces the table's retention
    without touching schema, quality, or pii blocks.
    """
    db, _, tbl = table.partition(".")
    if not tbl:
        db, tbl = "", table
    partial: dict[str, Any] = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "database_name": db,
        "table_name": tbl,
    }
    set_retention(partial, value, unit=unit, driver=driver, element=element)
    if physical_name:
        partial["schema"] = [{"name": f"{db}_{tbl}" if db else tbl,
                              "physicalName": physical_name, "properties": []}]
    return partial


def format_retention(contract: dict) -> str:
    """One-line human summary, e.g. ``90 d (driver=regulatory)`` or ``NA``."""
    r = get_retention(contract)
    if r["value"] == NA:
        return NA
    base = f"{r['value']} {r['unit']}".strip()
    extra = []
    if r.get("driver"):
        extra.append(f"driver={r['driver']}")
    if r.get("element"):
        extra.append(f"from={r['element']}")
    return f"{base} ({', '.join(extra)})" if extra else base
