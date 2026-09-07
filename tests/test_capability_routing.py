"""Capability routing unit tests."""

from __future__ import annotations

import pytest

from redibis.enrich.capability_routing import (
    RoutingError,
    assert_residency_allowed,
    build_role_bindings,
    default_binding,
    known_roles,
    resolve_model_binding,
)


def test_known_roles_include_planner_enrich_codegen():
    roles = known_roles()
    assert "agent.planner" in roles
    assert "contract.enrichment" in roles
    assert "codegen.generator" in roles
    assert "pii.refiner" in roles


def test_known_roles_include_gateway_and_text_refiner():
    roles = known_roles()
    assert "pii.text_refiner" in roles
    assert "gateway.toxicity" in roles
    assert "gateway.prompt_injection" in roles


def test_legacy_pii_llm_config_also_binds_text_refiner():
    """The legacy free-text ``pii.llm`` provider/model must also populate the
    new ``pii.text_refiner`` capability role so it shows up (and is usable)
    in the Settings capability matrix without a separate config block."""
    bindings = build_role_bindings(
        pii_llm={"provider": "ollama", "model_name": "llama3.3", "enabled": True},
    )
    refiner = resolve_model_binding("pii.text_refiner", bindings=bindings)
    assert refiner.provider == "ollama"
    assert refiner.model == "llama3.3"
    column_refiner = resolve_model_binding("pii.refiner", bindings=bindings)
    assert column_refiner.provider == "ollama"


def test_legacy_llm_defaults_map_to_enrichment_and_planner():
    bindings = build_role_bindings(
        llm_defaults={"provider": "sglang", "model": "default", "enabled": True},
        agentic_defaults={"planner_mode": "auto"},
    )
    enrich = resolve_model_binding("contract.enrichment", bindings=bindings)
    planner = resolve_model_binding("agent.planner", bindings=bindings)
    assert enrich.provider == "sglang"
    assert enrich.model == "default"
    assert planner.provider == "sglang"
    assert planner.model == "default"


def test_explicit_role_overrides_legacy_and_inherits():
    bindings = build_role_bindings(
        llm_defaults={"provider": "gemini", "model": "gemini-3.5-flash"},
        role_overrides={
            "agent.planner": {"provider": "sglang", "model": "default"},
            "agent.copilot": {"inherit": "agent.planner"},
            "codegen.generator": {
                "provider": "vllm",
                "model": "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
                "residency_policy": "local_only",
            },
        },
    )
    copilot = resolve_model_binding("agent.copilot", bindings=bindings)
    assert copilot.provider == "sglang"
    assert "inherit" in copilot.source
    codegen = resolve_model_binding("codegen.generator", bindings=bindings)
    assert codegen.provider == "vllm"
    assert codegen.residency_policy == "local_only"


def test_inherit_cycle_rejected():
    with pytest.raises(RoutingError, match="cycle"):
        build_role_bindings(
            role_overrides={
                "agent.planner": {"inherit": "agent.copilot"},
                "agent.copilot": {"inherit": "agent.planner"},
            }
        )


def test_local_only_residency_guard():
    binding = default_binding("codegen.generator")
    assert_residency_allowed(binding, "local")
    with pytest.raises(RoutingError, match="local residency"):
        assert_residency_allowed(binding, "public")


def test_request_override_wins():
    bindings = build_role_bindings(
        llm_defaults={"provider": "sglang", "model": "default"},
    )
    resolved = resolve_model_binding(
        "contract.enrichment",
        bindings=bindings,
        override_provider="openrouter",
        override_model="openai/gpt-4o-mini",
    )
    assert resolved.provider == "openrouter"
    assert resolved.model == "openai/gpt-4o-mini"
    assert resolved.source == "request"


def test_apply_routes_put_bumps_revision_and_syncs_legacy():
    from redibis.enrich.capability_routing import apply_routes_put, capture_routing_snapshot

    current = {"revision": 3, "llm_defaults": {"provider": "gemini", "model": "x"}}
    merged = apply_routes_put(
        current,
        {
            "settings": {
                "llm": {
                    "default": {"provider": "sglang", "model": "default"},
                    "roles": {
                        "agent.planner": {"provider": "sglang", "model": "default"},
                    },
                }
            }
        },
        expected_revision=3,
    )
    assert merged["revision"] == 4
    assert merged["llm_defaults"]["provider"] == "sglang"
    assert merged["agentic_defaults"]["planner_provider"] == "sglang"
    snap = capture_routing_snapshot(merged)
    assert snap.revision == 4
    assert resolve_model_binding("agent.planner", snapshot=snap).provider == "sglang"


def test_apply_routes_put_revision_mismatch():
    from redibis.enrich.capability_routing import apply_routes_put

    with pytest.raises(RoutingError, match="revision mismatch"):
        apply_routes_put(
            {"revision": 2},
            {"settings": {"llm": {"default": {}, "roles": {}}}},
            expected_revision=1,
        )
