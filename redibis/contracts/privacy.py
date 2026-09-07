"""
redibis.contracts.privacy — first-class ``privacy`` block on schema columns.

Canonical layout (governance artifact — no run-specific evidence)::

    privacy:
      classification: pii_personal
      masking_policy:
        default_strategy: fake
        reversible: false
        role_overrides: {admin: none, data_science: fake}

Durable entity typing lives on the column property (``entity_type`` field or
``entity:<TYPE>`` tag). Run-specific evidence (confidence, discovery engines,
decision_rule) is stored in ``ContractMetadataStore`` column telemetry — never
in ``privacy`` or the active contract spec.

Legacy ``classification_engine`` / ``pii`` / ``maskingPolicy`` keys are still
recognised on read and normalised on write.
"""

from __future__ import annotations

import copy
from typing import Any, Optional


def masking_policy_to_privacy_format(legacy: dict) -> dict:
    """Convert legacy ``maskingPolicy`` → ``masking_policy`` inside ``privacy``."""
    return {
        "default_strategy": legacy.get("default", "mask"),
        "reversible": bool(legacy.get("reversible", False)),
        "role_overrides": dict(legacy.get("roles") or {}),
    }


def masking_policy_from_privacy_format(privacy_masking: dict) -> dict:
    """Convert ``privacy.masking_policy`` → legacy ``maskingPolicy`` (interop)."""
    return {
        "default": privacy_masking.get("default_strategy", "mask"),
        "reversible": bool(privacy_masking.get("reversible", False)),
        **({"roles": privacy_masking["role_overrides"]}
           if privacy_masking.get("role_overrides") else {}),
    }


def build_classification_engine(pii_block: dict) -> dict:
    """Flatten legacy ``pii`` block → ``classification_engine`` (read compat)."""
    ce: dict[str, Any] = {"detected": bool(pii_block.get("detected", False))}
    if pii_block.get("entity_type"):
        ce["entity_type"] = pii_block["entity_type"]
    if pii_block.get("confidence") is not None:
        ce["confidence"] = pii_block["confidence"]
    evidence = dict(pii_block.get("evidence") or {})
    if evidence.get("decision_rule"):
        ce["decision_rule"] = evidence.pop("decision_rule")
    if evidence:
        ce["evidence"] = evidence
    return ce


def _classification_from_pii_block(pii_block: dict) -> Optional[str]:
    if not pii_block.get("detected"):
        return None
    from redibis.pii.sensitivity import classify_sensitivity

    entity = pii_block.get("entity_type")
    if entity:
        return classify_sensitivity(entity)
    return "pii_personal"


def build_privacy_block(
    pii_block: Optional[dict] = None,
    masking_policy: Optional[dict] = None,
    *,
    classification: Optional[str] = None,
) -> Optional[dict]:
    """Assemble the canonical policy-only ``privacy`` block."""
    pii_block = pii_block or {}
    cls = classification or _classification_from_pii_block(pii_block)
    if not cls and not masking_policy:
        return None
    privacy: dict[str, Any] = {}
    if cls:
        privacy["classification"] = cls
    if masking_policy:
        privacy["masking_policy"] = masking_policy_to_privacy_format(masking_policy)
    return privacy or None


def col_privacy_classification(prop: dict) -> str:
    """Return the column's privacy classification string, if any."""
    privacy = prop.get("privacy") or {}
    if privacy.get("classification"):
        return str(privacy["classification"])
    ce = privacy.get("classification_engine") or {}
    if ce.get("detected"):
        entity = ce.get("entity_type")
        if entity:
            from redibis.pii.sensitivity import classify_sensitivity
            return classify_sensitivity(entity)
        return "pii_personal"
    if _is_pii_classification(prop.get("classification")):
        return str(prop["classification"])
    legacy = prop.get("pii") or {}
    if legacy.get("detected"):
        return _classification_from_pii_block(legacy) or "pii_personal"
    return ""


def col_entity_type(prop: dict) -> str:
    """Durable entity type for a column (property field, tag, or legacy privacy)."""
    if prop.get("entity_type"):
        return str(prop["entity_type"])
    for tag in prop.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("entity:"):
            return tag.split(":", 1)[1]
    privacy = prop.get("privacy") or {}
    ce = privacy.get("classification_engine") or {}
    if ce.get("entity_type"):
        return str(ce["entity_type"])
    legacy = prop.get("pii") or {}
    if legacy.get("entity_type"):
        return str(legacy["entity_type"])
    return ""


def col_pii_engine(prop: dict) -> dict:
    """Synthesise a legacy engine-shaped dict for UI / interop (read-only)."""
    privacy = prop.get("privacy") or {}
    if privacy.get("classification_engine"):
        return privacy["classification_engine"]
    if prop.get("pii"):
        return build_classification_engine(prop["pii"])
    cls = privacy.get("classification") or ""
    if not cls and _is_pii_classification(prop.get("classification")):
        cls = str(prop["classification"])
    entity = col_entity_type(prop)
    if cls or entity:
        out: dict[str, Any] = {"detected": bool(cls)}
        if entity:
            out["entity_type"] = entity
        return out
    return {}


def col_masking_policy(prop: dict) -> dict:
    """Return ``privacy.masking_policy`` (or convert legacy ``maskingPolicy``)."""
    privacy = prop.get("privacy") or {}
    if privacy.get("masking_policy"):
        return privacy["masking_policy"]
    legacy = prop.get("maskingPolicy")
    if legacy:
        return masking_policy_to_privacy_format(legacy)
    return {}


def _is_pii_classification(value: Any) -> bool:
    return isinstance(value, str) and value.lower().startswith("pii")


#: classifications that never carry a PII read/masking signal, regardless of
#: any stale "detected" evidence flag elsewhere on the column.
_NON_PII_CLASSIFICATIONS = ("security_sensitive", "internal")


def column_is_pii(prop: dict) -> bool:
    """True when a column carries an active PII signal (privacy or legacy)."""
    cls = col_privacy_classification(prop)
    if cls in _NON_PII_CLASSIFICATIONS:
        return False

    entity = col_entity_type(prop)
    from redibis.pii.sensitivity import classify_sensitivity
    if entity and classify_sensitivity(entity) in _NON_PII_CLASSIFICATIONS:
        return False

    if cls and cls != "internal":
        return True
    if entity and classify_sensitivity(entity) != "internal":
        return True

    privacy = prop.get("privacy") or {}
    ce = privacy.get("classification_engine") or {}
    if ce.get("detected") is True:
        det_entity = ce.get("entity_type")
        if det_entity and classify_sensitivity(det_entity) in _NON_PII_CLASSIFICATIONS:
            return False
        return True
    if (prop.get("pii") or {}).get("detected") is True:
        legacy_entity = (prop.get("pii") or {}).get("entity_type")
        if legacy_entity and classify_sensitivity(legacy_entity) in _NON_PII_CLASSIFICATIONS:
            return False
        return True

    # A masking policy that merely says "passthrough" is not a PII signal --
    # it is what non-personal entities (e.g. ORGANIZATION) legitimately carry.
    masking = privacy.get("masking_policy") or {}
    if masking.get("default_strategy") not in (None, "passthrough"):
        return True
    legacy_masking = prop.get("maskingPolicy") or {}
    if legacy_masking.get("default") not in (None, "passthrough"):
        return True
    if any(t in ("pii", "gdpr_personal_data", "pii_indirect") for t in (prop.get("tags") or [])):
        return True
    return _is_pii_classification(prop.get("classification"))


def normalize_column_privacy(col: dict) -> bool:
    """Migrate legacy privacy into policy-only ``privacy``; strip duplicates."""
    changed = False
    legacy_pii = col.get("pii")
    legacy_mask = col.get("maskingPolicy")
    existing = col.get("privacy")

    if legacy_pii or legacy_mask:
        built = build_privacy_block(legacy_pii, legacy_mask)
        if built:
            if existing:
                merged = copy.deepcopy(existing)
                if built.get("classification") and not merged.get("classification"):
                    merged["classification"] = built["classification"]
                if built.get("masking_policy") and not merged.get("masking_policy"):
                    merged["masking_policy"] = built["masking_policy"]
                col["privacy"] = merged
            else:
                col["privacy"] = built
            changed = True

    if existing and existing.get("classification_engine"):
        ce = existing["classification_engine"]
        if not existing.get("classification") and ce.get("detected"):
            cls = _classification_from_pii_block({
                "detected": True,
                "entity_type": ce.get("entity_type"),
            })
            if cls:
                existing["classification"] = cls
                changed = True
        if ce.get("entity_type") and not col.get("entity_type"):
            col["entity_type"] = ce["entity_type"]
            changed = True
        existing.pop("classification_engine", None)
        changed = True

    if strip_legacy_privacy_keys(col):
        changed = True
    return changed


def strip_legacy_privacy_keys(col: dict) -> bool:
    """Remove duplicate legacy PII keys after ``privacy`` is canonical."""
    changed = False
    for key in ("pii", "maskingPolicy"):
        if key in col:
            col.pop(key, None)
            changed = True
    cps = col.get("customProperties")
    if isinstance(cps, list):
        kept = [cp for cp in cps
                if not (isinstance(cp, dict)
                        and cp.get("property") in ("pii", "maskingPolicy"))]
        if len(kept) != len(cps):
            changed = True
        if kept:
            col["customProperties"] = kept
        else:
            col.pop("customProperties", None)
    return changed


def strip_privacy_from_column(col: dict) -> bool:
    """Remove all privacy / PII signals from a column (demote to normal)."""
    changed = False
    if "privacy" in col:
        col.pop("privacy", None)
        changed = True
    if "entity_type" in col:
        col.pop("entity_type", None)
        changed = True
    if "tags" in col:
        kept = [t for t in col["tags"]
                if not (isinstance(t, str) and t.startswith("entity:"))]
        if len(kept) != len(col["tags"]):
            changed = True
        if kept:
            col["tags"] = kept
        else:
            col.pop("tags", None)
    if strip_legacy_privacy_keys(col):
        changed = True
    return changed


def apply_privacy_to_column(col: dict, payload: dict) -> bool:
    """Apply a PII column fragment (privacy block and/or legacy keys)."""
    changed = False
    if payload.get("privacy"):
        col["privacy"] = copy.deepcopy(payload["privacy"])
        changed = True
    elif payload.get("pii") or payload.get("maskingPolicy"):
        built = build_privacy_block(
            payload.get("pii"),
            payload.get("maskingPolicy"),
            classification=payload.get("classification"),
        )
        if built:
            col["privacy"] = built
            changed = True
        legacy_pii = payload.get("pii") or {}
        if legacy_pii.get("entity_type") and not col.get("entity_type"):
            col["entity_type"] = legacy_pii["entity_type"]
            changed = True
    for key in ("classification", "entity_type"):
        if key in payload:
            col[key] = copy.deepcopy(payload[key])
            changed = True
    if "tags" in payload:
        if payload.get("_replace_tags"):
            new_tags = list(payload["tags"])
            if col.get("tags") != new_tags:
                changed = True
            if new_tags:
                col["tags"] = new_tags
            else:
                col.pop("tags", None)
        else:
            existing = set(col.get("tags") or [])
            merged = sorted(existing | set(payload["tags"]))
            if merged != sorted(existing):
                changed = True
            col["tags"] = merged
    if payload.get("logicalType"):
        col["logicalType"] = payload["logicalType"]
        changed = True
    if payload.get("physicalType"):
        col["physicalType"] = payload["physicalType"]
        changed = True
    strip_legacy_privacy_keys(col)
    normalize_column_privacy(col)
    return changed


def iter_columns(contract: dict):
    """Yield every column property dict."""
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict):
                yield prop


# Value-free count rules — safe to keep on a PII column (no literal values embedded).
_SAFE_PII_QUALITY_RULES = {"missingCount", "duplicateCount", "nullCount"}


def quality_rule_is_value_bearing(rule: dict) -> bool:
    """True if a quality rule embeds literal column values (min/max/set/quantiles/lengths).

    These are the leak vectors: an LLM that sees the contract sees real PII values
    (e.g. a phone number's min/max/quantiles, a name's validValues).
    """
    if not isinstance(rule, dict):
        return False
    name = str(rule.get("rule") or "")
    if name and name not in _SAFE_PII_QUALITY_RULES:
        return True                       # validValues / regex / uniqueCount bounds / lengths …
    impl = rule.get("implementation") or {}
    etype = str(impl.get("expectation_type") or "")
    leaky = ("_min_", "_max_", "_between", "value_set", "values_to_be_in_set",
             "quantile", "median", "mean", "stdev", "value_lengths", "most_common")
    return any(m in etype for m in leaky)


def strip_quality_from_pii_columns(contract: dict, *, keep_safe_rules: bool = False) -> list[str]:
    """Remove value-bearing quality rules from every PII column (value-leak guard).

    min/max/quantile/validValues/length expectations on a PII column embed REAL PII values
    in the contract YAML — a leak risk if the contract is shared with an external LLM. By
    default the entire ``quality`` block is removed from PII columns; with ``keep_safe_rules``
    the value-free count rules (missingCount/duplicateCount) are kept. Table-level quality
    (rowCount / column set) is never touched. Returns the columns that were modified.
    """
    stripped: list[str] = []
    for col in iter_columns(contract):
        if not col.get("quality") or not column_is_pii(col):
            continue
        if keep_safe_rules:
            kept = [r for r in col["quality"] if not quality_rule_is_value_bearing(r)]
            if len(kept) != len(col["quality"]):
                stripped.append(str(col.get("name") or ""))
            if kept:
                col["quality"] = kept
            else:
                col.pop("quality", None)
        else:
            col.pop("quality", None)
            stripped.append(str(col.get("name") or ""))
    return stripped


# Masked skeletons for durable contract example_values (never real identifiers).
_PII_EXAMPLE_SKELETONS: dict[str, list[str]] = {
    "PHONE_NUMBER": ["+20 1# ### ####", "01##########"],
    "MSISDN": ["+20 1# ### ####", "01##########"],
    "EMAIL_ADDRESS": ["user@example.com", "name@domain.org"],
    "EMAIL": ["user@example.com", "name@domain.org"],
    "PERSON": ["First L.", "A. Last"],
    "IBAN_CODE": ["EG##BANK#######00000000000000"],
    "CREDIT_CARD": ["#### #### #### ####"],
    "EG_NATIONAL_ID": ["##############"],
    "PASSPORT": ["A########"],
    "IP_ADDRESS": ["192.0.2.###"],
    "LOCATION": ["City, Region"],
    "DATE_TIME": ["YYYY-MM-DD"],
    "DEFAULT": ["(masked example)", "(synthetic value)"],
}


def pii_example_skeleton(entity_type: Optional[str] = None) -> list[str]:
    """Return synthetic example_values safe for PII columns in shared contracts."""
    key = (entity_type or "").strip().upper()
    return list(_PII_EXAMPLE_SKELETONS.get(key) or _PII_EXAMPLE_SKELETONS["DEFAULT"])


def scrub_pii_text(text: str) -> str:
    """Redact value-like identifiers from business text on PII columns."""
    import re

    if not text:
        return text
    out = str(text)
    patterns = (
        (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[email redacted]"),
        (re.compile(r"\b\+?\d[\d\s\-()]{8,}\d\b"), "[phone redacted]"),
        (re.compile(r"\b\d{10,}\b"), "[identifier redacted]"),
    )
    for pattern, repl in patterns:
        out = pattern.sub(repl, out)
    return out


def scrub_pii_business_fields(biz: dict) -> dict:
    """Scrub definition/synonyms/example_values on a business block."""
    if not isinstance(biz, dict):
        return biz
    out = dict(biz)
    if out.get("definition"):
        out["definition"] = scrub_pii_text(str(out["definition"]))
    syns = out.get("synonyms")
    if isinstance(syns, list):
        out["synonyms"] = [scrub_pii_text(str(s)) for s in syns]
    return out


def scrub_pii_enrichment_output(contract: dict) -> list[str]:
    """Enforce output-leak policy on PII columns (examples, text, quality).

    Returns column names that were scrubbed.
    """
    scrubbed: list[str] = []
    for col in iter_columns(contract):
        if not column_is_pii(col):
            continue
        ce = col_pii_engine(col)
        entity = ce.get("entity_type") or col.get("entity_type")
        name = str(col.get("name") or "")
        biz = col.get("business")
        if not isinstance(biz, dict):
            continue
        new_biz = scrub_pii_business_fields(biz)
        if biz.get("example_values") is not None:
            new_biz["example_values"] = pii_example_skeleton(entity)
        if new_biz != biz:
            col["business"] = new_biz
            if name:
                scrubbed.append(name)
    strip_quality_from_pii_columns(contract)
    return scrubbed
