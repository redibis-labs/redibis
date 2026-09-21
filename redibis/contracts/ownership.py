"""ODCS ownership helpers — migrate legacy ``owner`` into top-level ``team``.

Installed ``OpenDataContractStandard`` (v3.1) has no top-level or schema-level
``owner`` field. Ownership is expressed only via top-level ``team`` members.
Redibis historically wrote ``owner: "<username>"``; this module normalizes
those values into ODCS-compliant ``team`` entries before validation/persist.
"""

from __future__ import annotations

import copy
from typing import Any, Optional


def _label_from_owner(owner: Any) -> str:
    if isinstance(owner, str):
        return owner.strip()
    if isinstance(owner, dict):
        return str(owner.get("name") or owner.get("username") or "").strip()
    return ""


def _member_labels(member: dict) -> set[str]:
    labels: set[str] = set()
    for key in ("name", "username"):
        val = str(member.get(key) or "").strip().lower()
        if val:
            labels.add(val)
    return labels


def owner_as_team_member(owner: Any, *, role: str = "owner") -> Optional[dict]:
    """Build a top-level ODCS ``team`` member from a legacy owner value."""
    label = _label_from_owner(owner)
    if not label:
        return None
    return {"name": label, "username": label, "role": role}


def ensure_team_member(contract: dict, member: dict) -> bool:
    """Append ``member`` to top-level ``team`` if no matching name/username exists."""
    if not isinstance(member, dict):
        return False
    wanted = _member_labels(member)
    if not wanted:
        return False
    team = list(contract.get("team") or [])
    for existing in team:
        if isinstance(existing, dict) and (_member_labels(existing) & wanted):
            return False
    team.append(copy.deepcopy(member))
    contract["team"] = team
    return True


def normalize_odcs_ownership(contract: dict) -> dict:
    """Migrate legacy ``owner`` fields into top-level ``team``; strip invalid keys.

    Mutates and returns ``contract``. Preserves ownership by promoting any
    top-level or schema-level ``owner`` (and schema-level ``team`` members)
    into the ODCS-compliant top-level ``team`` list, then removes the invalid
    fields that cause ``extra_forbidden`` under OpenDataContractStandard.
    """
    if not isinstance(contract, dict):
        return contract

    # Promote top-level legacy owner first.
    top_owner = contract.pop("owner", None)
    member = owner_as_team_member(top_owner)
    if member:
        ensure_team_member(contract, member)

    for schema_obj in contract.get("schema", []) or []:
        if not isinstance(schema_obj, dict):
            continue
        schema_owner = schema_obj.pop("owner", None)
        member = owner_as_team_member(schema_owner)
        if member:
            ensure_team_member(contract, member)
        # Schema-level team is also not in SchemaObject — fold into top-level.
        schema_team = schema_obj.pop("team", None)
        if isinstance(schema_team, list):
            for entry in schema_team:
                if isinstance(entry, dict):
                    ensure_team_member(contract, entry)
                else:
                    m = owner_as_team_member(entry)
                    if m:
                        ensure_team_member(contract, m)
        elif schema_team is not None:
            m = owner_as_team_member(schema_team)
            if m:
                ensure_team_member(contract, m)

    return contract
