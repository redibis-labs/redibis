"""
redibis.agents.capability_guard
================================
The "don't reinvent what redibis already does" seam.

When a user asks the agent to *generate code* (Ranger policy, masking script, a quality
check, a profiling routine…), we must only emit **external** code when redibis cannot
already do the job through its typed tool surface. Otherwise the agent should steer the
user to the native pipeline node — native paths are governed, audited, and contract-aware;
generated code is proposed-not-executed and carries egress/vuln/judge overhead.

Design (single source of truth, easy to maintain):
- ``CAPABILITY_KEYWORDS`` is the curated map of *native capability → trigger phrases*. This
  is the only thing most maintainers ever touch — add a phrase, the guard covers it.
- The capability *labels/descriptions* are pulled live from the node registry
  (``registry.list_nodes``) and the Tier-2 deep-profile catalogue (``deep_profile``), so the
  surface stays in sync with what the engine actually exposes. The keyword map only needs
  the phrasing users use, not a re-description of each node.

The guard is a **heuristic** in open core (keyword + registry match). A commercial LLM judge
can override it with a semantic decision, but the heuristic is the safe default: when in
doubt it prefers the native path and refuses to generate redundant code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Native capability → the phrases users use when they (wrongly) ask for code ──────────
# MAINTENANCE: this is the dial. Add a capability id (matching a registry node `type` or a
# Tier-2 capability id where possible) and the natural-language phrases that map to it.
CAPABILITY_KEYWORDS: dict[str, list[str]] = {
    "profile": [
        "profile", "profiling", "column stats", "statistics", "distribution",
        "null rate", "cardinality", "describe the data", "data summary",
    ],
    "quality": [
        "quality rule", "data quality", "expectation", "great expectations", "ge suite",
        "validation rule", "not null check", "uniqueness check", "range check", "row count check",
    ],
    "pii": [
        "detect pii", "pii scan", "find pii", "sensitive data", "personal data",
        "presidio", "gliner", "identify pii", "pii detection",
    ],
    "classify": [
        "classify", "classification", "policy tag", "sensitivity label", "data tagging",
        "apply tags", "tag columns",
    ],
    "mask": [
        "mask", "masking", "redact", "anonymize", "anonymise", "de-identify", "deidentify",
        "pseudonymize", "pseudonymise", "tokenize", "hash the", "encrypt the column",
        "fpe", "format preserving",
    ],
    "contract": [
        "data contract", "odcs", "build a contract", "generate contract", "contract spec",
    ],
    "publish": [
        "publish", "push to openmetadata", "push to atlas", "register in catalog", "catalog push",
    ],
    "sample": ["sample the table", "sampling", "take a sample", "row sample"],
    "source": [
        "connect to oracle", "connect to hive", "read from jdbc", "metastore", "load the table",
    ],
}


@dataclass
class NativeCapability:
    """A thing redibis can already do, with the registry label/description attached."""

    id: str
    label: str
    description: str
    via: str  # how to invoke it natively, e.g. "pipeline node: pii"
    matched_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "via": self.via,
            "matched_on": list(self.matched_on),
        }


def _registry_labels() -> dict[str, tuple[str, str]]:
    """{capability_id: (label, description)} from the live node registry (best-effort)."""
    out: dict[str, tuple[str, str]] = {}
    try:
        from redibis.agents.registry import list_nodes

        for entry in list_nodes():
            out[entry.type] = (entry.label, entry.description or "")
    except Exception:  # registry import optional at guard-eval time
        pass
    return out


def _tier2_labels() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    try:
        from redibis.agents.deep_profile import list_capabilities

        for c in list_capabilities():
            out[c.id] = (c.label, c.description or "")
    except Exception:
        pass
    return out


def native_capability_surface() -> list[dict[str, Any]]:
    """Full, self-describing capability list — for docs and planner context.

    Merges the curated keyword map with live registry/Tier-2 labels so the exported
    surface never drifts from the engine.
    """
    reg = _registry_labels()
    tier2 = _tier2_labels()
    surface: list[dict[str, Any]] = []
    for cap_id, phrases in CAPABILITY_KEYWORDS.items():
        label, desc = reg.get(cap_id) or tier2.get(cap_id) or (cap_id.title(), "")
        surface.append({
            "id": cap_id,
            "label": label,
            "description": desc,
            "phrases": list(phrases),
            "via": f"pipeline node: {cap_id}",
        })
    return surface


def match_native(intent: str) -> list[NativeCapability]:
    """Return the native capabilities the intent overlaps with (keyword heuristic)."""
    text = (intent or "").lower()
    reg = _registry_labels()
    tier2 = _tier2_labels()
    hits: list[NativeCapability] = []
    for cap_id, phrases in CAPABILITY_KEYWORDS.items():
        matched = [p for p in phrases if p in text]
        if not matched:
            continue
        label, desc = reg.get(cap_id) or tier2.get(cap_id) or (cap_id.title(), "")
        hits.append(NativeCapability(
            id=cap_id, label=label, description=desc,
            via=f"pipeline node: {cap_id}", matched_on=matched,
        ))
    return hits


@dataclass
class CodegenDecision:
    generate: bool
    reason: str
    native_matches: list[NativeCapability] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generate": self.generate,
            "reason": self.reason,
            "native_matches": [m.to_dict() for m in self.native_matches],
        }


def decide_codegen(intent: str, *, force_external: bool = False) -> CodegenDecision:
    """Policy: generate external code ONLY when redibis can't already do it.

    - ``force_external`` lets a user explicitly override (they know it's redundant and want
      the code anyway) — we still record the native matches in the reason for the audit trail.
    - Otherwise, any native overlap blocks generation and steers to the pipeline node.
    """
    matches = match_native(intent)
    if matches and not force_external:
        names = ", ".join(m.label for m in matches)
        return CodegenDecision(
            generate=False,
            reason=f"redibis already does this natively ({names}); use the pipeline instead of generating code.",
            native_matches=matches,
        )
    if matches and force_external:
        return CodegenDecision(
            generate=True,
            reason="user forced external code despite native capability overlap.",
            native_matches=matches,
        )
    return CodegenDecision(generate=True, reason="no native capability covers this intent.")
