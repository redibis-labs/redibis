"""External-code guard: generate code only when redibis can't do it natively."""

from __future__ import annotations

from redibis.agents.capability_guard import (
    CAPABILITY_KEYWORDS,
    decide_codegen,
    match_native,
    native_capability_surface,
)


def test_native_intent_blocks_codegen():
    d = decide_codegen("please mask the email column")
    assert d.generate is False
    assert any(m.id == "mask" for m in d.native_matches)


def test_non_native_intent_allows_codegen():
    d = decide_codegen("scrape an external REST API and load the json into a frame")
    assert d.generate is True
    assert d.native_matches == []


def test_force_external_overrides_but_records_match():
    d = decide_codegen("profile the table", force_external=True)
    assert d.generate is True
    assert any(m.id == "profile" for m in d.native_matches)


def test_match_native_is_case_insensitive():
    assert any(m.id == "pii" for m in match_native("DETECT PII in this data"))


def test_surface_is_self_describing():
    surface = native_capability_surface()
    ids = {c["id"] for c in surface}
    assert ids == set(CAPABILITY_KEYWORDS)
    for c in surface:
        assert c["id"] and c["phrases"] and c["via"]


def test_capability_ids_resolve_to_engine():
    """Each curated capability id should map to a registry node or Tier-2 capability.

    Guards against drift when a node is renamed/removed. Tolerant if the optional
    registry/Tier-2 modules aren't importable in a minimal test env.
    """
    try:
        from redibis.agents.registry import list_nodes
        node_types = {n.type for n in list_nodes()}
    except Exception:
        node_types = set()
    try:
        from redibis.agents.deep_profile import list_capabilities
        tier2 = {c.id for c in list_capabilities()}
    except Exception:
        tier2 = set()
    known = node_types | tier2
    if not known:
        return  # environment without the engine registry — skip drift check
    # capability ids are coarse verbs; at least the core ones must resolve
    core = {"profile", "contract", "mask", "publish", "sample", "source"}
    unresolved = {cid for cid in core if cid not in known}
    assert not unresolved, f"capability ids not in registry: {unresolved}"
