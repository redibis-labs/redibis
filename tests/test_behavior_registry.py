"""Tests for redibis.behavior.registry."""

from __future__ import annotations

import pytest

from redibis.behavior.registry import (
    BehaviorRegistry,
    EffectDescriptor,
    FactDescriptor,
    OperatorDescriptor,
    get_builtin_registry,
)
from redibis.behavior.models import HookStage


# ---------------------------------------------------------------------------
# Built-in registry
# ---------------------------------------------------------------------------

def test_builtin_registry_loads():
    reg = get_builtin_registry()
    assert reg.has_fact("column.name")
    assert reg.has_fact("verdict.entity")
    assert reg.has_fact("validators.phone.valid_rate")


def test_builtin_registry_operators():
    reg = get_builtin_registry()
    assert reg.has_operator("eq")
    assert reg.has_operator("matches")
    assert reg.has_operator("lt")
    assert reg.has_operator("between")


def test_builtin_registry_effects():
    reg = get_builtin_registry()
    assert reg.has_effect("core.verdict.set_entity")
    assert reg.has_effect("core.verdict.set_detected")
    assert reg.has_effect("core.review.require")
    assert reg.has_effect("core.threshold.set_for_run")


def test_builtin_registry_singleton():
    reg1 = get_builtin_registry()
    reg2 = get_builtin_registry()
    assert reg1 is reg2


# ---------------------------------------------------------------------------
# Custom registry
# ---------------------------------------------------------------------------

def test_register_fact():
    reg = BehaviorRegistry()
    reg.register_fact(FactDescriptor(
        id="custom.score",
        value_type="float",
        description="Custom score",
        supported_engines=("pii",),
        supported_stages=(HookStage.POST_VERDICT,),
        privacy_class="metadata",
        stability="experimental",
    ))
    assert reg.has_fact("custom.score")


def test_register_operator():
    reg = BehaviorRegistry()
    reg.register_operator(OperatorDescriptor(
        id="custom_op",
        description="A custom operator",
        input_types=("str",),
        expected_types=("str",),
    ))
    assert reg.has_operator("custom_op")


def test_register_effect():
    reg = BehaviorRegistry()
    reg.register_effect(EffectDescriptor(
        id="custom.effect",
        description="A custom effect",
        params_schema={"value": {"type": "string"}},
        supported_engines=("pii",),
        supported_stages=(HookStage.POST_VERDICT,),
        patch_operations=("custom.set",),
        side_effect="none",
        autonomy="auto",
    ))
    assert reg.has_effect("custom.effect")


def test_duplicate_non_builtin_rejected():
    reg = BehaviorRegistry()
    desc = FactDescriptor(
        id="x.score", value_type="float", description="x",
        supported_engines=("pii",), supported_stages=(HookStage.POST_VERDICT,),
        privacy_class="metadata", stability="stable",
    )
    reg.register_fact(desc)
    with pytest.raises(ValueError, match="Duplicate"):
        reg.register_fact(desc)


def test_builtin_cannot_be_replaced():
    reg = BehaviorRegistry()
    desc = FactDescriptor(
        id="column.name", value_type="str", description="test",
        supported_engines=("pii",), supported_stages=(HookStage.POST_VERDICT,),
        privacy_class="metadata", stability="stable",
    )
    reg.register_fact(desc, builtin=True)
    with pytest.raises(ValueError, match="built-in"):
        reg.register_fact(desc)   # non-builtin replacement rejected


def test_manifest_sha256_is_stable():
    reg = get_builtin_registry()
    sha1 = reg.manifest_sha256
    sha2 = reg.manifest_sha256
    assert sha1 == sha2
    assert len(sha1) == 64


def test_manifest_sha256_changes_after_registration():
    reg = BehaviorRegistry()
    sha1 = reg.manifest_sha256
    reg.register_fact(FactDescriptor(
        id="new.fact", value_type="bool", description="new",
        supported_engines=("pii",), supported_stages=(HookStage.POST_VERDICT,),
        privacy_class="metadata", stability="experimental",
    ))
    sha2 = reg.manifest_sha256
    assert sha1 != sha2


def test_catalogue_serializable():
    import json
    reg = get_builtin_registry()
    cat = reg.catalogue()
    # Must be JSON-serializable
    json.dumps(cat)
    assert "facts" in cat
    assert "operators" in cat
    assert "effects" in cat
