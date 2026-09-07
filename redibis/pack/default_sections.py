"""Assemble portable default-pack section documents (data only).

Serializes the shipped behavioral surface so ``export_default`` is more than
``config/redibis.yaml``: regex catalog, Presidio context tokens, phone locale,
NER labels/phrases, classification packs, masking regex library, collision /
coverage-gap tables, and portable LLM prompt templates (no keys).
"""

from __future__ import annotations

import importlib.resources
import json
from typing import Any

from redibis.config import RedibisConfig
from redibis.pack.config_allowlist import extract_portable_config
from redibis.pack.locale_builtin import ar_eg_tokens_document
from redibis.pii.ner_backend import DEFAULT_NER_LABELS, _GLINER_PHRASE_BY_CANONICAL
from redibis.pii.regex_catalog import (
    CATALOG,
    COLLISION_RESOLUTION_TABLE,
    COVERAGE_GAPS,
    _entry_to_dict,
)


def catalog_as_regex_overrides() -> dict[str, Any]:
    """Full shipped CATALOG as a self-contained ``locale/regex.yaml`` document."""
    add: dict[str, Any] = {}
    for name, entry in sorted(CATALOG.items()):
        payload = _entry_to_dict(name, entry)
        payload.pop("name", None)
        add[name] = payload
    return {"replace_all": True, "remove": [], "add": add}


def phone_document_from_config(cfg: RedibisConfig) -> dict[str, Any]:
    """Phone locale snapshot matching live ``RedibisConfig.pii`` defaults."""
    region = (cfg.pii.default_region or "").strip()
    return {
        "default_regions": [region] if region else [],
        "msisdn_prefixes": [str(p) for p in (cfg.pii.msisdn_prefixes or [])],
        "geofence": "egypt" if cfg.pii.geo_egypt_geofence else None,
    }


def default_ner_document(cfg: RedibisConfig | None = None) -> dict[str, Any]:
    """NER reference: labels, GLiNER phrases, confidence floor (no weights)."""
    cfg = cfg or RedibisConfig.default()
    labels = list(cfg.pii.ner.labels) if cfg.pii.ner.labels else list(DEFAULT_NER_LABELS)
    return {
        "active": None,
        "models": [],
        "labels": labels,
        "phrases": dict(_GLINER_PHRASE_BY_CANONICAL),
        "gliner_min": float(cfg.pii.thresholds.gliner_min),
        "type": cfg.pii.ner.type or "gliner",
    }


def collisions_document() -> dict[str, Any]:
    """Serialize ``COLLISION_RESOLUTION_TABLE`` for pack audit / overlay review."""
    out: dict[str, Any] = {}
    for group, rows in COLLISION_RESOLUTION_TABLE.items():
        out[group] = [
            {"pattern": name, "context_hints": list(hints)}
            for name, hints in rows
        ]
    return out


def coverage_gaps_document() -> dict[str, Any]:
    return {k: dict(v) for k, v in COVERAGE_GAPS.items()}


def classification_pack_bytes() -> dict[str, bytes]:
    """Builtin classification policy packs as raw YAML bytes."""
    from redibis.classification.policy_pack import list_builtin_packs

    pkg = importlib.resources.files("redibis.classification.packs")
    out: dict[str, bytes] = {}
    for name in list_builtin_packs():
        path = pkg / f"{name}.yaml"
        out[f"classification/packs/{name}.yaml"] = path.read_bytes()
    return out


def masking_regex_library_bytes() -> bytes:
    pkg = importlib.resources.files("redibis.masking")
    return (pkg / "regex_patterns.json").read_bytes()


def prompt_templates() -> dict[str, str]:
    """Portable prompt templates from the live Settings-backed store (+ PII extras)."""
    from redibis.enrich.prompt_store import get_enrich_prompt_store
    from redibis.pii.llm_refiner import _REFINER_SYSTEM
    from redibis.pii.tuning import _TUNING_PROMPT

    out = dict(get_enrich_prompt_store().export_pack_sections())
    out["assets/prompts/pii_refiner_system.md"] = _REFINER_SYSTEM.strip() + "\n"
    out["assets/prompts/pii_tuning.md"] = _TUNING_PROMPT.strip() + "\n"
    return out


def catalog_validator_names() -> list[str]:
    """Validators referenced by the shipped catalog (for ``requires.registries``)."""
    names: set[str] = set()
    for entry in CATALOG.values():
        if entry.requires_validator:
            names.add(entry.requires_validator)
    # Always include the primary luhn helper used by telecom patterns.
    names.add("validate_luhn")
    return sorted(names)


def build_default_sections(
    config: RedibisConfig | None = None,
) -> dict[str, Any]:
    """File map for the rich default pack (paths → mapping / str / bytes)."""
    cfg = config or RedibisConfig.default()
    sections: dict[str, Any] = {
        "config/redibis.yaml": extract_portable_config(cfg),
        "locale/tokens.yaml": ar_eg_tokens_document(),
        "locale/phone.yaml": phone_document_from_config(cfg),
        "locale/regex.yaml": catalog_as_regex_overrides(),
        "ner/models.yaml": default_ner_document(cfg),
        "assets/collisions.yaml": collisions_document(),
        "assets/coverage_gaps.yaml": coverage_gaps_document(),
        "assets/regex_patterns.json": json.loads(
            masking_regex_library_bytes().decode("utf-8")
        ),
    }
    sections.update(classification_pack_bytes())
    sections.update(prompt_templates())
    return sections


def default_contents_flags(sections: dict[str, Any]) -> dict[str, Any]:
    """Derive ``PackContents`` fields from the assembled section paths."""
    class_names = sorted(
        {
            p.rsplit("/", 1)[-1].removesuffix(".yaml").removesuffix(".yml")
            for p in sections
            if p.startswith("classification/packs/")
            and p.endswith((".yaml", ".yml"))
        }
    )
    return {
        "config": "config/redibis.yaml" in sections,
        "locale": any(p.startswith("locale/") for p in sections),
        "ner": "ner/models.yaml" in sections or "ner/models.yml" in sections,
        "classification": class_names,
        "behavior": [],
        "quality": [],
        "masking": [],
        "ner_weights": False,
    }
