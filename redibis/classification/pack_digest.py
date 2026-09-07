"""Compact classification-pack digest for LLM enrichment prompts."""

from __future__ import annotations

from typing import Any

from redibis.classification.policy_pack import ClassificationPolicy, get_builtin_pack

# Jurisdiction / role-routing axes — omitted from the LLM digest (token noise).
_DIGEST_SKIP_DOMAINS = frozenset({"RegulatoryCompliance"})


def build_pack_digest(policy: ClassificationPolicy) -> dict[str, Any]:
    """Project a policy pack into a compact, LLM-readable digest."""
    tag_vocabulary: dict[str, list[str]] = {}
    for domain, tags in sorted((policy.domains or {}).items()):
        if domain in _DIGEST_SKIP_DOMAINS or not tags:
            continue
        tag_vocabulary[domain] = list(tags)

    entity_to_tag = {
        entity: f"{mapping['domain']}:{mapping['tag']}"
        for entity, mapping in sorted((policy.entity_type_mapping or {}).items())
        if mapping.get("domain") and mapping.get("tag")
    }

    multi_classification: dict[str, list[str]] = {}
    for tag_name, entries in sorted((policy.cotag_matrix or {}).items()):
        if not entries:
            continue
        multi_classification[tag_name] = [
            f"{entry['domain']}:{entry['tag']}"
            for entry in entries
            if entry.get("domain") and entry.get("tag")
        ]

    return {
        "pack": policy.name,
        "tag_vocabulary": tag_vocabulary,
        "entity_to_tag": entity_to_tag,
        "multi_classification": multi_classification,
        "security_derivation": dict(policy.security_derivation or {}),
    }


def build_classification_pack_digest(pack_name: str) -> dict[str, Any]:
    """Load a pack by name and return its LLM digest."""
    try:
        return build_pack_digest(get_builtin_pack(pack_name))
    except Exception:
        return {"pack": pack_name, "error": "pack not found"}
