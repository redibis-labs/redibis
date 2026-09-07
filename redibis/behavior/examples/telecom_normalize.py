"""
redibis.behavior.examples.telecom_normalize
============================================
Reference plug-in: telecom identifier normalization metadata.

This module is the **reference example** for the plug-in SDK.  It ships
inside the ``redibis`` package so tests can import it directly.  In a real
deployment, a third-party package would expose these objects via the
``redibis.behavior_facts`` / ``redibis.behavior_actions`` entry-point groups.

Facts registered
----------------
``telecom.column.looks_normalized``
    ``bool`` — True when the column name suggests a normalized MSISDN/E.164
    field (e.g. ``msisdn_e164``, ``normalized_msisdn``).

``telecom.column.suggests_network_id``
    ``bool`` — True when the column name matches common CGI, LAC, or TAC
    patterns that indicate network-infrastructure identifiers (not PII).

Effects registered
------------------
``telecom.action.tag_network_id``
    When ``telecom.column.suggests_network_id`` is True and the current
    entity is PHONE_NUMBER or similar, set ``verdict.entity`` to
    ``NETWORK_ID`` and clear ``verdict.detected``.

Design constraints (all preserved)
-----------------------------------
- No I/O of any kind.
- Reads only registered metadata facts (column.name, verdict.entity).
- Returns a ``BehaviorPatch``; never writes to any store.
- Handler is deterministic, JSON-safe, context-immutable.
- ``side_effect`` is "none"; ``autonomy`` is "suggest-only".

Entry-point registration
------------------------
In a real distribution, add to ``pyproject.toml``::

    [project.entry-points."redibis.behavior_facts"]
    telecom_normalize_facts = "redibis.behavior.examples.telecom_normalize:register_facts"

    [project.entry-points."redibis.behavior_actions"]
    telecom_normalize_actions = "redibis.behavior.examples.telecom_normalize:register_actions"

Then list the distribution in ``BehaviorConfig.plugin_allowlist``::

    behavior:
      enabled: true
      plugin_allowlist:
        - redibis_telecom_normalize

For in-process testing (no package install), call ``register()`` directly::

    from redibis.behavior.examples.telecom_normalize import register
    register(registry)
"""

from __future__ import annotations

import re
from typing import Mapping

from redibis.behavior.models import (
    Authority,
    BehaviorContext,
    BehaviorPatch,
    HookStage,
    JSONValue,
    PatchOperation,
)
from redibis.behavior.registry import (
    BehaviorAction,
    BehaviorRegistry,
    EffectDescriptor,
    FactDescriptor,
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Column names that strongly suggest an already-normalised MSISDN / E.164 value.
_NORMALIZED_MSISDN_PATTERN = re.compile(
    r"\b(msisdn_e164|e164|normalized_msisdn|msisdn_normalized|"
    r"e164_msisdn|phone_e164|e164_phone)\b",
    re.IGNORECASE,
)

# Column names that suggest a network-infrastructure identifier, not user PII.
_NETWORK_ID_PATTERN = re.compile(
    r"\b(cgi|lac|tac|cell_id|cell_global_id|location_area_code|"
    r"tracking_area_code|enb_id|gnb_id|bts_id|rnc_id|sector_id|"
    r"site_id|tower_id|ran_id|node_id|base_station)\b",
    re.IGNORECASE,
)

# Entity types that may be mis-classified as phone numbers for network IDs.
_PHONE_LIKE_ENTITIES = frozenset({
    "PHONE_NUMBER",
    "MSISDN",
    "PHONE",
    "NETWORK_ID",   # prevent redundant re-tags
})

_SUPPORTED_STAGES = (HookStage.POST_VERDICT,)
_SUPPORTED_ENGINES = ("pii",)


# ---------------------------------------------------------------------------
# Fact descriptors
# ---------------------------------------------------------------------------

FACT_LOOKS_NORMALIZED = FactDescriptor(
    id="telecom.column.looks_normalized",
    value_type="bool",
    description=(
        "True when the column name suggests a normalized MSISDN / E.164 field "
        "(e.g. msisdn_e164, normalized_msisdn). "
        "Derived from column.name metadata only — no raw values inspected."
    ),
    supported_engines=tuple(_SUPPORTED_ENGINES),
    supported_stages=tuple(_SUPPORTED_STAGES),
    privacy_class="metadata",
    stability="experimental",
    nullable=True,
)

FACT_SUGGESTS_NETWORK_ID = FactDescriptor(
    id="telecom.column.suggests_network_id",
    value_type="bool",
    description=(
        "True when the column name matches known CGI, LAC, or TAC patterns "
        "that indicate network-infrastructure identifiers (not subscriber PII). "
        "Derived from column.name only — no raw values inspected."
    ),
    supported_engines=tuple(_SUPPORTED_ENGINES),
    supported_stages=tuple(_SUPPORTED_STAGES),
    privacy_class="metadata",
    stability="experimental",
    nullable=True,
)


# ---------------------------------------------------------------------------
# Effect descriptor + handler
# ---------------------------------------------------------------------------

EFFECT_TAG_NETWORK_ID = EffectDescriptor(
    id="telecom.action.tag_network_id",
    description=(
        "When the column name suggests a network identifier (CGI/LAC/TAC), "
        "propose setting verdict.entity to NETWORK_ID and clearing "
        "verdict.detected. Suggest-only: requires downstream approval."
    ),
    params_schema={},
    supported_engines=tuple(_SUPPORTED_ENGINES),
    supported_stages=tuple(_SUPPORTED_STAGES),
    patch_operations=("verdict.entity", "verdict.detected"),
    side_effect="none",
    autonomy="suggest-only",
)


class _TagNetworkIdAction:
    """Handler for ``telecom.action.tag_network_id``."""

    descriptor = EFFECT_TAG_NETWORK_ID

    def apply(
        self,
        context: BehaviorContext,
        params: Mapping[str, JSONValue],
    ) -> BehaviorPatch:
        """
        Propose NETWORK_ID classification when column.name matches network-ID
        patterns.  Returns an empty patch when the facts do not apply.

        Reads only: column.name, verdict.entity, telecom.column.suggests_network_id.
        """
        column_name: str = str(context.facts.get("column.name") or "")
        if not column_name:
            return BehaviorPatch()

        already_network_id = (
            str(context.facts.get("verdict.entity") or "") == "NETWORK_ID"
        )
        if already_network_id:
            return BehaviorPatch()

        # Check the telecom fact if already computed in facts dict;
        # fall back to the pattern if the fact provider is not wired.
        suggests_network_id = context.facts.get("telecom.column.suggests_network_id")
        if suggests_network_id is None:
            suggests_network_id = bool(_NETWORK_ID_PATTERN.search(column_name))

        if not suggests_network_id:
            return BehaviorPatch()

        reason = (
            f"Column name {column_name!r} matches network-infrastructure patterns "
            "(CGI/LAC/TAC); proposing NETWORK_ID entity instead of PHONE_NUMBER."
        )
        ops = (
            PatchOperation(
                op="set",
                path="verdict.entity",
                value="NETWORK_ID",
                authority=Authority.APPROVED_POLICY,
                reason=reason,
                rule_id="telecom.network_id_by_column_name",
                policy_id="telecom_normalize",
            ),
            PatchOperation(
                op="set",
                path="verdict.detected",
                value=True,
                authority=Authority.APPROVED_POLICY,
                reason=reason,
                rule_id="telecom.network_id_by_column_name",
                policy_id="telecom_normalize",
            ),
        )
        return BehaviorPatch(operations=ops, reasons=(reason,))


TAG_NETWORK_ID_ACTION = _TagNetworkIdAction()


# ---------------------------------------------------------------------------
# Fact providers
# ---------------------------------------------------------------------------

def _provide_looks_normalized(context: BehaviorContext) -> bool | None:
    """Compute telecom.column.looks_normalized from context.facts["column.name"]."""
    name = str(context.facts.get("column.name") or "")
    if not name:
        return None
    return bool(_NORMALIZED_MSISDN_PATTERN.search(name))


def _provide_suggests_network_id(context: BehaviorContext) -> bool | None:
    """Compute telecom.column.suggests_network_id from context.facts["column.name"]."""
    name = str(context.facts.get("column.name") or "")
    if not name:
        return None
    return bool(_NETWORK_ID_PATTERN.search(name))


# ---------------------------------------------------------------------------
# Entry-point callables (used by importlib.metadata entry points)
# ---------------------------------------------------------------------------

def register_facts() -> list[tuple[FactDescriptor, object]]:
    """
    Entry-point callable for group ``redibis.behavior_facts``.

    Returns list of ``(FactDescriptor, provider_callable)`` tuples.
    The provider is called at runtime to populate ``context.facts`` if the
    adapter's context builder uses it; adapters that already populate the
    fact can ignore the provider.
    """
    return [
        (FACT_LOOKS_NORMALIZED,    _provide_looks_normalized),
        (FACT_SUGGESTS_NETWORK_ID, _provide_suggests_network_id),
    ]


def register_actions() -> list[tuple[EffectDescriptor, BehaviorAction]]:
    """
    Entry-point callable for group ``redibis.behavior_actions``.

    Returns list of ``(EffectDescriptor, handler)`` tuples.
    """
    return [
        (EFFECT_TAG_NETWORK_ID, TAG_NETWORK_ID_ACTION),
    ]


# ---------------------------------------------------------------------------
# Direct registration helper (for tests / in-process use)
# ---------------------------------------------------------------------------

def register(registry: BehaviorRegistry) -> None:
    """
    Register all descriptors and handlers into *registry* directly.

    Useful for in-process testing without a package install::

        from redibis.behavior.examples.telecom_normalize import register
        from redibis.behavior.registry import BehaviorRegistry

        reg = BehaviorRegistry()
        register(reg)
        assert reg.has_fact("telecom.column.looks_normalized")
    """
    for desc, _provider in register_facts():
        registry.register_fact(desc)
    for desc, handler in register_actions():
        registry.register_effect(desc, handler=handler)
