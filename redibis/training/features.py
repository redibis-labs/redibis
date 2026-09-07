"""Build value-free column feature vectors for training export."""

from __future__ import annotations

import re
from typing import Any, Optional

from redibis.contracts.privacy import col_entity_type, col_privacy_classification

_TOKEN_SPLIT = re.compile(r"[_\-\s.]+")


def name_tokens(column: str) -> list[str]:
    parts = [p.lower() for p in _TOKEN_SPLIT.split(column or "") if p]
    return parts or ([column.lower()] if column else [])


def _num(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def build_column_features(
    *,
    table: str,
    column: str,
    prop: Optional[dict[str, Any]] = None,
    telemetry: Optional[dict[str, Any]] = None,
    table_domain: str = "",
    format_signatures: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Assemble Track-A (value-free) features from contract + telemetry.

    Never includes cell samples. Format signatures must already be shapes/regexes,
    not sampled strings.
    """
    prop = prop or {}
    tel = telemetry or {}
    features: dict[str, Any] = {
        "table": table,
        "column_name": column,
        "name_tokens": name_tokens(column),
    }
    logical = prop.get("logicalType") or prop.get("logical_type") or ""
    physical = prop.get("physicalType") or prop.get("physical_type") or ""
    if logical:
        features["logical_type"] = str(logical)
    if physical:
        features["physical_type"] = str(physical)

    # Structural stats if present on the property / telemetry (already aggregated).
    for key in ("null_rate", "distinct_ratio", "avg_len", "phone_valid_rate"):
        n = _num(prop.get(key) if key in prop else tel.get(key))
        if n is not None:
            features[key] = n

    for key in ("length_hist", "charclass_profile", "regex_hits", "ner_hits"):
        val = prop.get(key) if key in prop else tel.get(key)
        if val not in (None, "", [], {}):
            features[key] = val

    conf = _num(tel.get("confidence"))
    if conf is not None:
        features["engine_confidence"] = conf
    if tel.get("discovery_engines"):
        features["discovery_engines"] = list(tel.get("discovery_engines") or [])
    if tel.get("decision_rule"):
        features["decision_rule"] = str(tel["decision_rule"])

    for score_key, feat_key in (
        ("presidio_score", "presidio_score"),
        ("gliner_score", "ner_score"),
        ("regex_score", "regex_score"),
        ("phone_score", "phone_score"),
        ("llm_score", "llm_score"),
    ):
        n = _num(tel.get(score_key))
        if n is not None:
            features[feat_key] = n

    ner_label = tel.get("gliner_label") or tel.get("ner_label") or ""
    if ner_label:
        features["ner_label"] = str(ner_label)
    eng_entity = tel.get("entity_type") or ""
    if eng_entity:
        features["engine_entity_type"] = str(eng_entity)

    sigs = format_signatures
    if not sigs and isinstance(tel.get("format_signatures"), list):
        sigs = [str(s) for s in tel["format_signatures"]]
    if sigs:
        features["format_signatures"] = list(sigs)

    cls = col_privacy_classification(prop) or str(prop.get("classification") or "")
    if cls:
        features["contract_classification"] = cls
    et = col_entity_type(prop)
    if et:
        features["contract_entity_type"] = et

    if table_domain:
        features["table_domain"] = str(table_domain)
    elif "." in table:
        features["table_domain"] = table.split(".", 1)[0]

    return features
