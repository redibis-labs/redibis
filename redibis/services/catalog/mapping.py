"""Neutral ODCS → catalog semantics (shared by all catalog backends)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Union

import yaml

from redibis.contracts.privacy import column_is_pii, col_pii_engine

_SENSITIVE_ENTITIES = frozenset({
    "NATIONAL_ID", "EG_NATIONAL_ID", "SSN", "PASSPORT", "CREDIT_CARD", "IBAN",
    "BANK_ACCOUNT", "DRIVERS_LICENSE", "MEDICAL_RECORD", "BIOMETRIC",
})


@dataclass
class TableSemantics:
    table: str
    database: str
    table_name: str
    description: str
    schema_name: str


@dataclass
class ColumnSemantics:
    name: str
    description: str
    business_name: str
    logical_type: str
    physical_type: str
    is_pii: bool
    entity_type: str
    tags: list[str] = field(default_factory=list)
    # Policy-engine tags as "Domain:Tag" keys (resolved only; no suggestions).
    policy_tags: list[str] = field(default_factory=list)


def split_physical_name(table: str) -> tuple[str, str]:
    """``telecom.customers`` → (``telecom``, ``customers``)."""
    parts = table.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"table must be schema.table, got {table!r}")
    return parts[0], parts[1]


def resolve_contract_table(
    contract: dict,
    *,
    override: Optional[str] = None,
    path: Optional[Union[str, Path]] = None,
) -> str:
    """Derive ``schema.table`` from an ODCS document or explicit override."""
    if override:
        table = override.strip()
        split_physical_name(table)
        return table

    physical = (contract.get("physicalName") or "").strip()
    if physical and "." in physical:
        split_physical_name(physical)
        return physical

    for obj in contract.get("schema", []) or []:
        if isinstance(obj, dict):
            pn = (obj.get("physicalName") or "").strip()
            if pn and "." in pn:
                split_physical_name(pn)
                return pn

    db = (contract.get("database_name") or "").strip()
    tbl = (contract.get("table_name") or "").strip()
    if db and tbl:
        return f"{db}.{tbl}"

    if path is not None:
        stem = Path(path).stem
        if "." in stem:
            split_physical_name(stem)
            return stem

    raise ValueError(
        "Cannot resolve schema.table from contract — set physicalName, "
        "database_name/table_name, or pass --table"
    )


def load_contract_file(path: Union[str, Path]) -> dict:
    """Load an ODCS contract from a ``.yaml`` / ``.yml`` / ``.json`` file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Contract file not found: {p}")

    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        contract = json.loads(text)
    else:
        contract = yaml.safe_load(text)

    if not isinstance(contract, dict):
        raise ValueError(f"Contract file must contain a mapping: {p}")
    return contract


_CONTRACT_GLOBS = ("*.yaml", "*.yml", "*.json")


def iter_contract_files(
    root: Union[str, Path],
    *,
    glob: Optional[str] = None,
    recursive: bool = False,
) -> Iterator[Path]:
    """Yield contract files under ``root`` (single file passes through)."""
    p = Path(root)
    if p.is_file():
        yield p.resolve()
        return
    if not p.is_dir():
        raise NotADirectoryError(f"Not a contract file or directory: {p}")

    patterns = (glob,) if glob else _CONTRACT_GLOBS
    seen: set[Path] = set()
    for pattern in patterns:
        matches = p.rglob(pattern) if recursive else p.glob(pattern)
        for match in sorted(matches):
            if match.is_file():
                resolved = match.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield resolved


def schema_object(contract: dict, table: str) -> dict:
    for obj in contract.get("schema", []) or []:
        if isinstance(obj, dict):
            pn = obj.get("physicalName") or ""
            if pn == table or not pn:
                return obj
    raise ValueError(f"no schema object for table {table!r}")


def _column_description(prop: dict) -> str:
    """Prefer explicit column ``description``, else enriched ``business.definition``.

    Enrichment writes steward text under ``properties[].business.definition``.
    Catalog publishers map that text to OM column descriptions when a top-level
    ``description`` is absent so enriched contracts remain visible in Explore.
    """
    desc = (prop.get("description") or "").strip()
    if desc:
        return desc
    business = prop.get("business")
    if isinstance(business, dict):
        return str(business.get("definition") or "").strip()
    return ""


def column_semantics(
    prop: dict,
    *,
    policy_tags: Optional[list[str]] = None,
) -> ColumnSemantics:
    ce = col_pii_engine(prop)
    entity = str(ce.get("entity_type") or "UNKNOWN").upper()
    desc = _column_description(prop)
    if column_is_pii(prop):
        note = f"PII ({entity}): cite column existence only; do not quote values."
        if note.lower() not in desc.lower():
            desc = f"{desc} {note}".strip() if desc else note
    return ColumnSemantics(
        name=prop["name"],
        description=desc[:280],
        business_name=(prop.get("businessName") or "").strip(),
        logical_type=prop.get("logicalType") or "string",
        physical_type=prop.get("physicalType") or "",
        is_pii=column_is_pii(prop),
        entity_type=entity,
        tags=[str(t) for t in (prop.get("tags") or [])],
        policy_tags=list(policy_tags or []),
    )


def policy_tag_map(classification_results: Optional[list]) -> dict[str, list[str]]:
    """Map column name → resolved policy tag keys (excludes LawfulIntercept)."""
    out: dict[str, list[str]] = {}
    if not classification_results:
        return out
    for result in classification_results:
        # Serialized rows from classification_result_rows
        if isinstance(result, dict):
            col = str(result.get("column") or "")
            keys = []
            for raw in result.get("tags") or []:
                raw_s = str(raw)
                if raw_s.endswith(":LawfulIntercept") or raw_s == "LawfulIntercept":
                    continue
                keys.append(raw_s)
            if col and keys:
                out[col] = keys
            continue

        col = getattr(result, "column", "") or ""
        if not col:
            continue
        keys: list[str] = []
        for tag in getattr(result, "resolved_tags", []) or []:
            domain = getattr(tag, "domain", "") or ""
            name = getattr(tag, "tag", "") or ""
            if domain == "RegulatoryCompliance" and name == "LawfulIntercept":
                continue
            if hasattr(tag, "ref"):
                keys.append(tag.ref().key())
            elif domain and name:
                keys.append(f"{domain}:{name}")
        if col and keys:
            out[col] = keys
    return out


def iter_columns(
    contract: dict,
    table: str,
    *,
    classification_results: Optional[list] = None,
) -> Iterator[ColumnSemantics]:
    obj = schema_object(contract, table)
    by_col = policy_tag_map(classification_results)
    for prop in obj.get("properties", []) or []:
        if not isinstance(prop, dict) or not prop.get("name"):
            continue
        yield column_semantics(prop, policy_tags=by_col.get(prop["name"], []))


def table_semantics(contract: dict, table: str) -> TableSemantics:
    database, table_name = split_physical_name(table)
    obj = schema_object(contract, table)
    raw_desc = contract.get("description") or obj.get("description") or ""
    if isinstance(raw_desc, dict):
        raw_desc = (
            raw_desc.get("description")
            or raw_desc.get("purpose")
            or ""
        )
    desc = str(raw_desc).strip()
    if not desc:
        desc = f"ODCS contract for {table}"
    return TableSemantics(
        table=table,
        database=database,
        table_name=table_name,
        description=desc,
        schema_name=obj.get("name") or table_name,
    )


def pii_sensitivity_label(entity_type: str) -> str:
    """Return ``PII.Sensitive`` or ``PII.NonSensitive`` for a detected entity type."""
    if entity_type in _SENSITIVE_ENTITIES:
        return "PII.Sensitive"
    return "PII.NonSensitive"


def column_catalog_tag_names(col: ColumnSemantics, *, include_tags: bool) -> list[str]:
    """Neutral tag/classification names derived from ODCS column semantics."""
    if not include_tags:
        return []
    names: list[str] = []
    if col.is_pii:
        names.append(pii_sensitivity_label(col.entity_type))
        names.append(f"Redibis.{col.entity_type}")
    for raw in col.tags:
        if raw.lower() in ("pii", "gdpr_personal_data"):
            continue
        names.append(f"Redibis.{raw}")
    for policy in col.policy_tags:
        # Domain:Tag → RedibisPolicy.Domain__Tag (OM Classification.Tag FQN)
        safe = policy.replace(":", "__")
        names.append(f"RedibisPolicy.{safe}")
    return names


def redibis_glossary_name(database: str) -> str:
    """Per-database OpenMetadata glossary name: ``Redibis_{database}`` (sanitized)."""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", database or "")
    return f"Redibis_{safe}"
