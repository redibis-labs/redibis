"""
ODCS Contract Merger — generic N-way field-level merge.

Merge invariants (locked architecture decisions):
  1. IDENTITY FIELDS are write-once and locked after first creation:
       - contract_uuid
       - database_name
       - table_name
     If incoming partial carries different identity values, raise
     IdentityConflictError.

  2. TAGS merge as SET UNION (never replaced) — lets multiple workflows
     contribute tags without overwriting each other.
       PII workflow adds [pii, gdpr_personal_data]
       Business import adds [regulated, billing]
       Result: [billing, gdpr_personal_data, pii, regulated]

  3. ALL OTHER FIELDS are last-writer-wins. The incoming partial fully
     replaces the corresponding section if present, including:
       - logicalType, physicalType, description, businessName
       - classification, required, unique
       - quality (the entire list)
       - pii (the entire block)

  4. NEW COLUMNS in incoming → appended. Columns only in existing → preserved.

  5. PROVENANCE / pii_summary / last_updated — NOT written into the contract
     spec. Operational telemetry is stored in ContractMetadataStore (sidecar /
     metadata bucket). Use make_provenance_entry() at upsert time.

  6. VERSION — bumped semver-style on each merge (1.0.0 → 1.0.1).
"""

from __future__ import annotations
import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import yaml


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────

class IdentityConflictError(ValueError):
    """
    Raised when an incoming contract carries identity fields
    (contract_uuid, database_name, table_name) that differ from
    the existing active contract.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Provenance entry
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProvenanceEntry:
    """One row in contract.provenance — appended on every merge."""
    workflow:           str        # 'ge' | 'pii' | 'business' | 'manual'
    run_id:             str        # human-readable timestamp
    run_uuid:           str        # unique per pipeline run
    timestamp:          str        # ISO 8601 UTC
    contributed_fields: list[str]  # ['quality', 'description', ...]

    def to_dict(self) -> dict:
        return {
            "workflow":            self.workflow,
            "run_id":              self.run_id,
            "run_uuid":            self.run_uuid,
            "timestamp":           self.timestamp,
            "contributed_fields":  self.contributed_fields,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

# Fields that may NEVER be replaced once set. These uniquely identify the
# contract entity; mismatches must raise IdentityConflictError.
_IDENTITY_FIELDS: tuple[str, ...] = (
    "contract_uuid",
    "database_name",
    "table_name",
)

# Per-column fields that fully replace if present in incoming.
_REPLACEABLE_COLUMN_FIELDS: tuple[str, ...] = (
    "logicalType",
    "physicalType",
    "description",
    "businessName",
    "classification",
    "required",
    "unique",
    "quality",        # entire list replaces
    "pii",            # entire block replaces
    "maskingPolicy",  # entire block replaces
)


def _bump_version(version: str) -> str:
    """Patch-level bump: '1.0.0' → '1.0.1'."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return "1.0.1"
    major, minor, patch = parts
    return f"{major}.{minor}.{int(patch) + 1}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_contract(source: Union[str, Path, dict, None]) -> Optional[dict]:
    """Load a contract from a path, YAML string, or dict. None passes through."""
    if source is None:
        return None
    if isinstance(source, dict):
        return copy.deepcopy(source)
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.exists() and path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            try:
                return yaml.safe_load(text)
            except yaml.constructor.ConstructorError:
                try:
                    raw = yaml.load(text, Loader=yaml.UnsafeLoader)
                except Exception:
                    import re
                    stripped = re.sub(
                        r'!!python/object/apply:numpy\.\S+\s*\n'
                        r'(?:\s+-\s+!!python/object/apply:numpy\.\S+\s*\n)*'
                        r'(?:\s+-\s+.*\n)*',
                        'null\n', text,
                    )
                    raw = yaml.safe_load(stripped)
                from redibis.store.storage_backend import _sanitize_for_yaml
                return _sanitize_for_yaml(raw)
        # Treat as YAML string
        return yaml.safe_load(str(source))
    raise TypeError(f"Unsupported contract source type: {type(source)}")


def _detect_contributed_fields(partial: dict) -> list[str]:
    """
    Inspects an incoming partial contract and returns a sorted list of
    semantic field names it contributes (used for provenance entries).
    """
    contributed: set[str] = set()

    if not partial:
        return []

    # Top-level
    if "description"   in partial:                     contributed.add("description")
    if "pii_summary"   in partial:                     contributed.add("pii_summary")
    if "slaProperties" in partial:                     contributed.add("slaProperties")
    if "terms"         in partial:                     contributed.add("terms")
    if "schema"        in partial and partial["schema"]:
        for table in partial["schema"]:
            if "quality" in table:                     contributed.add("table_quality")
            for col in table.get("properties", []) or []:
                for key in _REPLACEABLE_COLUMN_FIELDS:
                    if key in col:
                        contributed.add(key)
                if "tags" in col:                      contributed.add("tags")

    return sorted(contributed)


# ─────────────────────────────────────────────────────────────────────────────
# Public merge function (single existing + single incoming)
# ─────────────────────────────────────────────────────────────────────────────

def merge_two_contracts(
    existing: Optional[dict],
    incoming: dict,
    workflow: str       = "manual",
    run_id:   str       = "",
    run_uuid: str       = "",
) -> dict:
    """
    Merge a single incoming partial contract into an existing active contract.

    Args:
        existing : The current active contract dict, or None for first creation.
        incoming : The partial contract dict produced by a workflow.
        workflow : Source workflow name for provenance ('ge'|'pii'|'business'|'manual').
        run_id   : Human-readable run identifier (for provenance).
        run_uuid : Unique run identifier (for provenance).

    Returns:
        dict : The merged contract.

    Raises:
        IdentityConflictError : If incoming carries identity fields that
                                differ from existing.
    """
    if not isinstance(incoming, dict):
        raise TypeError(f"incoming must be dict, got {type(incoming)}")

    incoming = copy.deepcopy(incoming)
    run_id   = run_id   or _utc_now_iso()
    run_uuid = run_uuid or str(uuid.uuid4())

    # ── First-time creation ──────────────────────────────────────────────
    if existing is None:
        merged = copy.deepcopy(incoming)

        # Ensure identity fields are populated
        if "contract_uuid" not in merged or not merged["contract_uuid"]:
            merged["contract_uuid"] = str(uuid.uuid4())
        if "database_name" not in merged:
            merged["database_name"] = ""
        if "table_name" not in merged:
            merged["table_name"]    = ""

        merged.setdefault("version", "1.0.0")
        return merged

    # ── Incremental merge ────────────────────────────────────────────────
    merged = copy.deepcopy(existing)

    # Identity validation — locked fields cannot change
    for id_field in _IDENTITY_FIELDS:
        if id_field in incoming and incoming[id_field]:
            existing_val = merged.get(id_field, "")
            incoming_val = incoming[id_field]
            if existing_val and existing_val != incoming_val:
                raise IdentityConflictError(
                    f"Identity field '{id_field}' mismatch: "
                    f"existing={existing_val!r} vs incoming={incoming_val!r}. "
                    "Identity fields are locked after first write."
                )
            # If existing is empty, populate from incoming
            if not existing_val:
                merged[id_field] = incoming_val

    # Top-level non-identity replaceable fields (spec only — no telemetry)
    for field in ("description", "slaProperties", "terms"):
        if field in incoming:
            merged[field] = incoming[field]

    # Schema-level merge (table-level + columns)
    if "schema" in incoming and incoming["schema"]:
        merged.setdefault("schema", [])
        for inc_table in incoming["schema"]:
            existing_table = _find_or_create_table(merged["schema"], inc_table)
            _merge_table(existing_table, inc_table)

    merged["version"] = _bump_version(merged.get("version", "1.0.0"))
    return merged


def make_provenance_entry(
    incoming: dict,
    workflow: str,
    run_id: str = "",
    run_uuid: str = "",
) -> dict:
    """Build one provenance row for ContractMetadataStore (not the contract spec)."""
    return ProvenanceEntry(
        workflow=workflow,
        run_id=run_id or _utc_now_iso(),
        run_uuid=run_uuid or str(uuid.uuid4()),
        timestamp=_utc_now_iso(),
        contributed_fields=_detect_contributed_fields(incoming),
    ).to_dict()


def _find_or_create_table(tables: list, incoming_table: dict) -> dict:
    """
    Locate matching table in existing list. Match priority:
      1. physicalName (canonical identifier — 'db.table')
      2. name (logical/schema identifier)

    This handles the case where two workflows produce different schema names
    (e.g. 'telecom_customers' vs 'telecom.customers') for the same physical
    table — physicalName is the source of truth.
    """
    inc_physical = incoming_table.get("physicalName")
    inc_name     = incoming_table.get("name")

    # Match by physicalName first
    if inc_physical:
        for t in tables:
            if t.get("physicalName") == inc_physical:
                return t

    # Fallback to name match
    if inc_name:
        for t in tables:
            if t.get("name") == inc_name:
                # If incoming has a physicalName and existing doesn't, populate it
                if inc_physical and not t.get("physicalName"):
                    t["physicalName"] = inc_physical
                return t

        # Also check: if existing table's physicalName matches incoming's name
        # (handles case where one side uses 'db.table' as name, the other as physicalName)
        for t in tables:
            if t.get("physicalName") == inc_name:
                return t

    # No match — append new
    new_table = {"name": inc_name or inc_physical, "properties": []}
    if inc_physical:
        new_table["physicalName"] = inc_physical
    tables.append(new_table)
    return new_table


def _merge_table(existing_table: dict, incoming_table: dict) -> None:
    """Merge a single incoming table into an existing table dict in-place."""
    # Replaceable table-level fields
    for field in ("physicalName", "logicalType"):
        if field in incoming_table and incoming_table[field]:
            existing_table[field] = incoming_table[field]

    # Description
    if "description" in incoming_table:
        existing_table["description"] = incoming_table["description"]

    # Table-level quality (replaces wholesale)
    if "quality" in incoming_table:
        existing_table["quality"] = incoming_table["quality"]

    # Columns
    existing_props = existing_table.setdefault("properties", [])
    inc_props      = incoming_table.get("properties", []) or []
    existing_by_name = {p["name"]: p for p in existing_props if "name" in p}

    for inc_col in inc_props:
        col_name = inc_col.get("name")
        if not col_name:
            continue
        if col_name in existing_by_name:
            _merge_column(existing_by_name[col_name], inc_col)
        else:
            existing_props.append(copy.deepcopy(inc_col))


def _merge_column(existing_col: dict, incoming_col: dict) -> None:
    """
    Merge a single incoming column entry into an existing one in-place.

    Universal rules:
      - 'name' (column identity) : NEVER overwritten by merge
      - 'tags'                   : SET UNION (sorted)
      - everything else          : last-writer-wins (any field in incoming replaces existing)

    This permissive policy means any custom ODCS extension fields
    (authoritativeDefinitions, customAttributes, customQuality, etc.)
    propagate through merges automatically without requiring a registry update.
    """
    # Tags — SET UNION
    if "tags" in incoming_col:
        existing_tags = set(existing_col.get("tags", []) or [])
        incoming_tags = set(incoming_col.get("tags", []) or [])
        existing_col["tags"] = sorted(existing_tags | incoming_tags)

    # Every other field — last-writer-wins (skip 'name' and 'tags', already handled)
    for field, value in incoming_col.items():
        if field in ("name", "tags"):
            continue
        existing_col[field] = copy.deepcopy(value)


# ─────────────────────────────────────────────────────────────────────────────
# Public N-way merge entry point
# ─────────────────────────────────────────────────────────────────────────────

def merge_odcs_contracts(
    contracts: list[Union[str, Path, dict]],
    output_path: Optional[Union[str, Path]] = None,
    workflow:    str = "manual",
    run_id:      str = "",
    run_uuid:    str = "",
) -> dict:
    """
    Merge N ODCS contracts left-to-right, returning the merged result.

    The first contract becomes the base; each subsequent contract is merged
    in via merge_two_contracts(). Identity fields are validated on every
    merge step and conflicts raise IdentityConflictError.

    Args:
        contracts    : List of dicts, paths, or YAML strings (mixed allowed).
                       Order matters — later contracts override earlier ones
                       for replaceable fields. Tags accumulate via union
                       across all merges.
        output_path  : Optional path to write the merged YAML. If None, no
                       file is written; the merged dict is returned only.
        workflow     : Source workflow tag for the final provenance entry.
        run_id       : Run identifier for provenance.
        run_uuid     : Run UUID for provenance.

    Returns:
        dict : The merged contract.

    Example:
        merged = merge_odcs_contracts(
            contracts = [
                "s3-fetched-active.yaml",   # current active state
                "ge_partial.yaml",           # this run's GE output
                "biz_glossary.yaml",         # human-curated definitions
            ],
            output_path = "merged.yaml",
            workflow    = "ge",
            run_id      = "2026-05-09_14-32-18",
        )
    """
    if not contracts:
        raise ValueError("merge_odcs_contracts: at least one contract required")

    # Load all sources, dropping None/empty
    loaded: list[dict] = []
    for src in contracts:
        c = _load_contract(src)
        if c is not None:
            loaded.append(c)

    if not loaded:
        raise ValueError("merge_odcs_contracts: no valid contracts loaded")

    # Left-fold merge
    merged = loaded[0]
    # Ensure base has identity + version + provenance
    merged = merge_two_contracts(
        existing = None,
        incoming = merged,
        workflow = workflow,
        run_id   = run_id,
        run_uuid = run_uuid,
    )

    for incoming in loaded[1:]:
        merged = merge_two_contracts(
            existing = merged,
            incoming = incoming,
            workflow = workflow,
            run_id   = run_id,
            run_uuid = run_uuid,
        )

    # Optional write
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                merged,
                f,
                default_flow_style = False,
                sort_keys          = False,
                allow_unicode      = True,
            )

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("── merge_contracts.py smoke test ─────────────────────────")

    # Test 1: First-time creation populates identity
    base = {
        "apiVersion":    "v3.0.1",
        "kind":          "DataContract",
        "name":          "telecom_customers_contract",
        "database_name": "telecom",
        "table_name":    "customers",
        "schema": [{
            "name": "telecom_customers",
            "properties": [
                {"name": "phone", "tags": ["pii"], "pii": {"detected": True}},
            ],
        }],
    }
    m1 = merge_two_contracts(None, base, workflow="pii", run_id="r1", run_uuid="u1")
    assert "contract_uuid" in m1 and m1["contract_uuid"]
    assert m1["database_name"] == "telecom"
    assert m1["table_name"]    == "customers"
    assert m1["version"]       == "1.0.0"
    assert len(m1["provenance"]) == 1
    print("✓ First-time creation populates identity + provenance")

    # Test 2: Incremental merge bumps version, replaces fields, unions tags
    incoming = {
        "schema": [{
            "name": "telecom_customers",
            "properties": [
                {
                    "name": "phone",
                    "tags": ["regulated", "billing"],         # union
                    "description": "Customer mobile phone",   # replaces (didn't exist)
                    "businessName": "Mobile Phone",            # replaces (didn't exist)
                },
            ],
        }],
    }
    m2 = merge_two_contracts(m1, incoming, workflow="business", run_id="r2", run_uuid="u2")
    assert m2["version"] == "1.0.1"
    assert m2["last_updated_by_workflow"] == "business"
    phone = m2["schema"][0]["properties"][0]
    # Tags merged via union
    assert set(phone["tags"]) == {"pii", "regulated", "billing"}
    # PII block from base preserved
    assert phone["pii"]["detected"] is True
    # New fields added
    assert phone["description"]  == "Customer mobile phone"
    assert phone["businessName"] == "Mobile Phone"
    # Two provenance entries now
    assert len(m2["provenance"]) == 2
    print("✓ Incremental merge — tags unioned, fields replaced, version bumped")

    # Test 3: Identity conflict raises
    bad = {
        "contract_uuid": "00000000-0000-0000-0000-000000000000",  # different
        "database_name": "telecom",
        "table_name":    "customers",
    }
    try:
        merge_two_contracts(m2, bad, workflow="manual")
        raise AssertionError("Expected IdentityConflictError")
    except IdentityConflictError as e:
        assert "contract_uuid" in str(e)
    print("✓ Identity conflict raises IdentityConflictError")

    # Test 4: Replaceable fields fully replace
    replace_pii = {
        "schema": [{
            "name": "telecom_customers",
            "properties": [
                {
                    "name": "phone",
                    "pii": {"detected": True, "entity_type": "PHONE_NUMBER", "confidence": 0.95},
                },
            ],
        }],
    }
    m3 = merge_two_contracts(m2, replace_pii, workflow="pii", run_id="r3", run_uuid="u3")
    phone3 = m3["schema"][0]["properties"][0]
    assert phone3["pii"]["entity_type"] == "PHONE_NUMBER"
    assert phone3["pii"]["confidence"]  == 0.95
    # Tags from previous merges still present
    assert "pii" in phone3["tags"]
    assert "regulated" in phone3["tags"]
    print("✓ pii block fully replaced; existing tags preserved")

    # Test 5: New columns appended
    add_col = {
        "schema": [{
            "name": "telecom_customers",
            "properties": [
                {"name": "email", "logicalType": "string"},
            ],
        }],
    }
    m4 = merge_two_contracts(m3, add_col, workflow="ge", run_id="r4", run_uuid="u4")
    cols = [p["name"] for p in m4["schema"][0]["properties"]]
    assert "phone" in cols and "email" in cols
    print("✓ New columns appended; existing preserved")

    # Test 6: N-way merge entry point
    merged_n = merge_odcs_contracts(
        contracts = [base, incoming, replace_pii, add_col],
        workflow  = "manual",
    )
    assert merged_n["version"] == "1.0.3"  # 3 incremental merges after base
    assert len(merged_n["schema"][0]["properties"]) == 2
    print("✓ N-way merge: base + 3 incoming → version 1.0.3, 2 columns")

    print("\n✅ All merge_contracts smoke tests passed")