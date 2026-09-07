"""
Tests for redibis.behavior.plugins (Phase 7 — Trusted fact/action plug-in SDK).

Covers:
- Allow-list rejection (unallow-listed packages are blocked).
- Built-in ID protection (third-party cannot replace built-ins).
- Entry-point format validation (wrong return types, missing descriptors, etc.).
- Side-effect class restriction.
- Successful registration when allow-listed (via direct entry-point simulation).
- Conformance suite on the reference telecom_normalize plug-in.
- plugin_manifest summary helper.
"""

from __future__ import annotations

import importlib.metadata
import types
import unittest.mock as mock
from typing import Mapping

import pytest

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.examples.telecom_normalize import (
    EFFECT_TAG_NETWORK_ID,
    FACT_LOOKS_NORMALIZED,
    FACT_SUGGESTS_NETWORK_ID,
    TAG_NETWORK_ID_ACTION,
    register,
    register_actions,
    register_facts,
)
from redibis.behavior.models import (
    Authority,
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    HookStage,
    JSONValue,
    PatchOperation,
)
from redibis.behavior.plugin_conformance import (
    assert_bounded_execution,
    assert_deterministic,
    assert_json_safe_patch,
    assert_no_context_mutation,
    assert_no_raw_value_requirement,
    assert_only_declared_ops,
    run_conformance_suite,
)
from redibis.behavior.plugins import (
    PluginRejected,
    _GROUP_ACTIONS,
    _GROUP_FACTS,
    apply_plugins_to_registry,
    load_plugins,
    plugin_manifest,
)
from redibis.behavior.registry import (
    BehaviorRegistry,
    EffectDescriptor,
    FactDescriptor,
    get_builtin_registry,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _fresh_registry() -> BehaviorRegistry:
    """Return a fresh registry seeded with built-ins."""
    from redibis.behavior.registry import _build_builtin_registry
    return _build_builtin_registry()


def _make_ctx(
    facts: dict | None = None,
    column: str = "cell_id",
    stage: HookStage = HookStage.POST_VERDICT,
) -> BehaviorContext:
    return BehaviorContext(
        run_id="test-run",
        table="telecom.cdr",
        column=column,
        engine="pii",
        stage=stage,
        facts=facts or {
            "column.name": column,
            "verdict.entity": "PHONE_NUMBER",
            "verdict.detected": True,
            "verdict.confidence": 0.72,
        },
        capabilities=BehaviorCapabilities(
            allowed_patch_operations=frozenset({
                "verdict.set_entity",
                "verdict.set_detected",
                "verdict.set_confidence",
                "review.require",
                "review.add_reason",
            }),
        ),
        registry_manifest_sha256="",
    )


def _make_fake_ep(
    name: str,
    group: str,
    dist_name: str,
    fn: object,
    version: str = "1.0.0",
) -> mock.MagicMock:
    """Build a mock EntryPoint that ``ep.load()`` returns *fn*."""
    dist = mock.MagicMock()
    dist.metadata = {"Name": dist_name, "Version": version}
    dist.name = dist_name
    dist.read_text.return_value = None

    ep = mock.MagicMock(spec=importlib.metadata.EntryPoint)
    ep.name  = name
    ep.group = group
    ep.value = f"{dist_name}:{name}"
    ep.dist  = dist
    ep.load.return_value = fn
    return ep


def _patch_entry_points(eps: list, group: str):
    """Context manager that patches importlib.metadata.entry_points for one group."""
    def fake_entry_points(group: str):
        return [ep for ep in eps if ep.group == group]
    return mock.patch(
        "redibis.behavior.plugins.importlib.metadata.entry_points",
        side_effect=fake_entry_points,
    )


# ---------------------------------------------------------------------------
# 1. Allow-list enforcement
# ---------------------------------------------------------------------------

class TestAllowlistEnforcement:

    def test_empty_allowlist_loads_nothing(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=[])
        reg    = _fresh_registry()
        reports = load_plugins(config, reg)
        assert reports == []

    def test_unallowlisted_package_is_rejected(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["trusted_pkg"])
        reg    = _fresh_registry()

        ep = _make_fake_ep(
            "bad_facts", _GROUP_FACTS, "untrusted_pkg",
            lambda: [(FACT_SUGGESTS_NETWORK_ID, None)],
        )
        with _patch_entry_points([ep], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        assert len(reports) == 1
        r = reports[0]
        assert r["status"] == "rejected"
        assert "not in plugin_allowlist" in r["reject_reason"]
        assert not reg.has_fact("telecom.column.suggests_network_id")

    def test_allowlisted_package_is_accepted(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_telecom_pkg"])
        reg    = _fresh_registry()

        ep = _make_fake_ep(
            "facts_ep", _GROUP_FACTS, "my_telecom_pkg",
            register_facts,
        )
        with _patch_entry_points([ep], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        assert any(r["status"] == "ok" for r in reports), reports
        assert reg.has_fact("telecom.column.looks_normalized")
        assert reg.has_fact("telecom.column.suggests_network_id")

    def test_multiple_distributions_selective_allow(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["good_pkg"])
        reg    = _fresh_registry()

        ep_good = _make_fake_ep("g_facts", _GROUP_FACTS, "good_pkg",  register_facts)
        ep_bad  = _make_fake_ep("b_facts", _GROUP_FACTS, "evil_pkg",  register_facts)

        with _patch_entry_points([ep_good, ep_bad], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        statuses = {r["distribution"]: r["status"] for r in reports}
        assert statuses.get("good_pkg") == "ok"
        assert statuses.get("evil_pkg") == "rejected"


# ---------------------------------------------------------------------------
# 2. Built-in ID protection
# ---------------------------------------------------------------------------

class TestBuiltinIdProtection:

    def _make_clobbering_fact(self, target_id: str) -> FactDescriptor:
        return FactDescriptor(
            id=target_id,
            value_type="bool",
            description="Attempt to replace built-in",
            supported_engines=("pii",),
            supported_stages=(HookStage.POST_VERDICT,),
            privacy_class="metadata",
            stability="experimental",
        )

    def test_cannot_replace_builtin_fact(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        bad_desc = self._make_clobbering_fact("verdict.detected")
        ep = _make_fake_ep(
            "bad_ep", _GROUP_FACTS, "my_pkg",
            lambda: [(bad_desc, None)],
        )
        with _patch_entry_points([ep], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        r = next((r for r in reports if r["ep_name"] == "bad_ep"), None)
        assert r is not None
        assert r["status"] == "rejected"
        assert "built-in fact" in r["reject_reason"]

    def test_cannot_replace_builtin_effect(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        bad_desc = EffectDescriptor(
            id="core.verdict.set_entity",  # built-in
            description="Clobber attempt",
            params_schema={},
            supported_engines=("pii",),
            supported_stages=(HookStage.POST_VERDICT,),
            patch_operations=("verdict.set_entity",),
            side_effect="none",
            autonomy="suggest-only",
        )
        ep = _make_fake_ep(
            "bad_action_ep", _GROUP_ACTIONS, "my_pkg",
            lambda: [(bad_desc, TAG_NETWORK_ID_ACTION)],
        )
        with _patch_entry_points([ep], _GROUP_ACTIONS):
            reports = load_plugins(config, reg)

        r = next((r for r in reports if r["ep_name"] == "bad_action_ep"), None)
        assert r is not None
        assert r["status"] == "rejected"
        assert "built-in effect" in r["reject_reason"]

    def test_non_namespaced_id_rejected(self):
        """Third-party IDs must contain '.'."""
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        bad_desc = FactDescriptor(
            id="flatid",  # no namespace
            value_type="bool",
            description="Missing namespace",
            supported_engines=("pii",),
            supported_stages=(HookStage.POST_VERDICT,),
            privacy_class="metadata",
            stability="experimental",
        )
        ep = _make_fake_ep(
            "flat_ep", _GROUP_FACTS, "my_pkg",
            lambda: [(bad_desc, None)],
        )
        with _patch_entry_points([ep], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        r = next((r for r in reports if r["ep_name"] == "flat_ep"), None)
        assert r is not None
        assert r["status"] == "rejected"
        assert "namespaced" in r["reject_reason"]


# ---------------------------------------------------------------------------
# 3. Entry-point format validation
# ---------------------------------------------------------------------------

class TestEntrypointFormatValidation:

    def test_non_callable_ep_rejected(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        ep = _make_fake_ep("not_callable", _GROUP_FACTS, "my_pkg", "not_a_function")
        with _patch_entry_points([ep], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        r = reports[0]
        assert r["status"] == "rejected"

    def test_ep_returning_wrong_type_rejected(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        ep = _make_fake_ep("wrong_type", _GROUP_FACTS, "my_pkg", lambda: "not_a_list")
        with _patch_entry_points([ep], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        r = reports[0]
        assert r["status"] == "rejected"

    def test_ep_returning_empty_list_rejected(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        ep = _make_fake_ep("empty_ep", _GROUP_FACTS, "my_pkg", lambda: [])
        with _patch_entry_points([ep], _GROUP_FACTS):
            reports = load_plugins(config, reg)

        r = reports[0]
        assert r["status"] == "rejected"
        assert "no descriptors" in r["reject_reason"]

    def test_action_ep_none_handler_rejected(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        ep = _make_fake_ep(
            "null_handler", _GROUP_ACTIONS, "my_pkg",
            lambda: [(EFFECT_TAG_NETWORK_ID, None)],  # handler=None
        )
        with _patch_entry_points([ep], _GROUP_ACTIONS):
            reports = load_plugins(config, reg)

        r = reports[0]
        assert r["status"] == "rejected"
        assert "handler" in r["reject_reason"]


# ---------------------------------------------------------------------------
# 4. Side-effect class restriction
# ---------------------------------------------------------------------------

class TestSideEffectRestriction:

    def _make_effect(self, side_effect: str) -> EffectDescriptor:
        return EffectDescriptor(
            id="custom.some.effect",
            description="Test effect",
            params_schema={},
            supported_engines=("pii",),
            supported_stages=(HookStage.POST_VERDICT,),
            patch_operations=("verdict.set_entity",),
            side_effect=side_effect,
            autonomy="suggest-only",
        )

    @pytest.mark.parametrize("se", ["write", "external"])
    def test_restricted_side_effect_rejected(self, se):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        desc = self._make_effect(se)
        ep = _make_fake_ep(
            "bad_se", _GROUP_ACTIONS, "my_pkg",
            lambda: [(desc, TAG_NETWORK_ID_ACTION)],
        )
        with _patch_entry_points([ep], _GROUP_ACTIONS):
            reports = load_plugins(config, reg)

        r = reports[0]
        assert r["status"] == "rejected"
        assert "side_effect" in r["reject_reason"]

    @pytest.mark.parametrize("se", ["none", "review"])
    def test_allowed_side_effect_accepted(self, se):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        desc = self._make_effect(se)
        ep = _make_fake_ep(
            f"ok_se_{se}", _GROUP_ACTIONS, "my_pkg",
            lambda: [(desc, TAG_NETWORK_ID_ACTION)],
        )
        with _patch_entry_points([ep], _GROUP_ACTIONS):
            reports = load_plugins(config, reg)

        r = reports[0]
        assert r["status"] == "ok", r.get("reject_reason")


# ---------------------------------------------------------------------------
# 5. apply_plugins_to_registry
# ---------------------------------------------------------------------------

class TestApplyPluginsToRegistry:

    def test_returns_registry(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=[])
        reg    = _fresh_registry()
        result = apply_plugins_to_registry(config, reg)
        assert result is reg

    def test_registers_actions_and_facts(self):
        config = BehaviorConfig(enabled=True, plugin_allowlist=["my_pkg"])
        reg    = _fresh_registry()

        ep_facts   = _make_fake_ep("f_ep", _GROUP_FACTS,   "my_pkg", register_facts)
        ep_actions = _make_fake_ep("a_ep", _GROUP_ACTIONS, "my_pkg", register_actions)

        def fake_eps(group):
            if group == _GROUP_FACTS:   return [ep_facts]
            if group == _GROUP_ACTIONS: return [ep_actions]
            return []

        with mock.patch(
            "redibis.behavior.plugins.importlib.metadata.entry_points",
            side_effect=fake_eps,
        ):
            apply_plugins_to_registry(config, reg)

        assert reg.has_fact("telecom.column.looks_normalized")
        assert reg.has_fact("telecom.column.suggests_network_id")
        assert reg.has_effect("telecom.action.tag_network_id")
        assert reg.get_action("telecom.action.tag_network_id") is TAG_NETWORK_ID_ACTION


# ---------------------------------------------------------------------------
# 6. Reference plug-in: telecom_normalize
# ---------------------------------------------------------------------------

class TestTelecomNormalizePlugin:

    def setup_method(self):
        self.reg = BehaviorRegistry()
        register(self.reg)

    def test_facts_registered(self):
        assert self.reg.has_fact("telecom.column.looks_normalized")
        assert self.reg.has_fact("telecom.column.suggests_network_id")

    def test_effect_registered(self):
        assert self.reg.has_effect("telecom.action.tag_network_id")

    def test_action_handler_wired(self):
        assert self.reg.get_action("telecom.action.tag_network_id") is not None

    def test_network_id_columns_produce_patch(self):
        for col in ("cell_id", "lac", "cgi", "tac", "base_station"):
            ctx   = _make_ctx(column=col)
            patch = TAG_NETWORK_ID_ACTION.apply(ctx, {})
            assert len(patch.operations) == 2, f"Expected patch for column {col!r}"
            paths = {op.path for op in patch.operations}
            assert "verdict.entity" in paths
            assert "verdict.detected" in paths

    def test_non_network_columns_produce_no_patch(self):
        for col in ("customer_name", "email", "amount", "credit_card"):
            ctx   = _make_ctx(column=col)
            patch = TAG_NETWORK_ID_ACTION.apply(ctx, {})
            assert patch.operations == (), f"Expected empty patch for {col!r}"

    def test_already_network_id_produces_no_patch(self):
        ctx = _make_ctx(facts={
            "column.name": "cell_id",
            "verdict.entity": "NETWORK_ID",
            "verdict.detected": False,
            "verdict.confidence": 0.0,
        })
        patch = TAG_NETWORK_ID_ACTION.apply(ctx, {})
        assert patch.operations == ()

    def test_empty_column_name_produces_no_patch(self):
        ctx = _make_ctx(facts={
            "column.name": "",
            "verdict.entity": "PHONE_NUMBER",
            "verdict.detected": True,
            "verdict.confidence": 0.72,
        })
        patch = TAG_NETWORK_ID_ACTION.apply(ctx, {})
        assert patch.operations == ()

    def test_normalized_msisdn_fact_descriptors(self):
        assert FACT_LOOKS_NORMALIZED.privacy_class == "metadata"
        assert FACT_SUGGESTS_NETWORK_ID.privacy_class == "metadata"
        assert FACT_LOOKS_NORMALIZED.stability == "experimental"

    def test_effect_descriptor_safe_autonomy(self):
        assert EFFECT_TAG_NETWORK_ID.side_effect == "none"
        assert EFFECT_TAG_NETWORK_ID.autonomy == "suggest-only"


# ---------------------------------------------------------------------------
# 7. Conformance suite
# ---------------------------------------------------------------------------

class TestConformanceSuite:

    def _sample_ctx(self):
        return _make_ctx(column="cgi")

    def test_full_suite_passes(self):
        ctx    = self._sample_ctx()
        errors = run_conformance_suite(
            TAG_NETWORK_ID_ACTION,
            EFFECT_TAG_NETWORK_ID,
            ctx,
            {},
        )
        assert errors == [], "\n".join(errors)

    def test_assert_deterministic_passes(self):
        ctx = self._sample_ctx()
        assert_deterministic(TAG_NETWORK_ID_ACTION, ctx, {}, n=5)

    def test_assert_json_safe_patch(self):
        ctx   = self._sample_ctx()
        patch = TAG_NETWORK_ID_ACTION.apply(ctx, {})
        assert_json_safe_patch(patch)

    def test_assert_no_context_mutation(self):
        ctx = self._sample_ctx()
        assert_no_context_mutation(TAG_NETWORK_ID_ACTION, ctx, {})

    def test_assert_only_declared_ops(self):
        ctx   = self._sample_ctx()
        patch = TAG_NETWORK_ID_ACTION.apply(ctx, {})
        assert_only_declared_ops(patch, EFFECT_TAG_NETWORK_ID)

    def test_assert_no_raw_value_requirement(self):
        ctx = self._sample_ctx()
        assert_no_raw_value_requirement(TAG_NETWORK_ID_ACTION, ctx, {})

    def test_assert_bounded_execution(self):
        ctx = self._sample_ctx()
        assert_bounded_execution(TAG_NETWORK_ID_ACTION, ctx, {}, max_ms=500)

    def test_non_deterministic_handler_caught(self):
        """A handler that returns random patches fails determinism check."""
        import random

        class _Flaky:
            descriptor = EFFECT_TAG_NETWORK_ID

            def apply(self, ctx, params):
                if random.random() < 0.5:
                    return BehaviorPatch()
                return BehaviorPatch(
                    operations=(
                        PatchOperation(
                            op="set", path="verdict.set_entity",
                            value="RANDOM",
                            authority=Authority.APPROVED_POLICY,
                            reason="flaky",
                        ),
                    )
                )

        ctx = self._sample_ctx()
        with pytest.raises(AssertionError, match="not deterministic"):
            # Run many iterations to reliably catch the flaky handler.
            for _ in range(20):
                assert_deterministic(_Flaky(), ctx, {}, n=10)

    def test_undeclared_op_caught(self):
        class _BadOps:
            descriptor = EFFECT_TAG_NETWORK_ID

            def apply(self, ctx, params):
                return BehaviorPatch(
                    operations=(
                        PatchOperation(
                            op="set", path="undeclared.secret_field",
                            value="x",
                            authority=Authority.APPROVED_POLICY,
                            reason="bad op",
                        ),
                    )
                )

        ctx   = self._sample_ctx()
        patch = _BadOps().apply(ctx, {})
        with pytest.raises(AssertionError, match="undeclared operation path"):
            assert_only_declared_ops(patch, EFFECT_TAG_NETWORK_ID)

    def test_context_mutation_caught(self):
        ctx = BehaviorContext(
            run_id="r1",
            table="t",
            column="c",
            engine="pii",
            stage=HookStage.POST_VERDICT,
            facts={"column.name": "cgi"},   # mutable dict — handler can mutate
            capabilities=BehaviorCapabilities(),
            registry_manifest_sha256="",
        )

        class _Mutating:
            descriptor = EFFECT_TAG_NETWORK_ID

            def apply(self, ctx, params):
                ctx.facts["injected"] = "evil"  # type: ignore[index]
                return BehaviorPatch()

        with pytest.raises(AssertionError, match="mutated ctx.facts"):
            assert_no_context_mutation(_Mutating(), ctx, {})


# ---------------------------------------------------------------------------
# 8. plugin_manifest helper
# ---------------------------------------------------------------------------

class TestPluginManifest:

    def test_manifest_structure(self):
        reports = [
            {"distribution": "a", "version": "1.0", "ep_name": "ep1",
             "ep_group": _GROUP_FACTS, "registered_ids": ["a.fact"],
             "digest": "abc", "status": "ok", "reject_reason": None},
            {"distribution": "b", "version": "1.0", "ep_name": "ep2",
             "ep_group": _GROUP_ACTIONS, "registered_ids": [],
             "digest": "", "status": "rejected",
             "reject_reason": "not in plugin_allowlist"},
        ]
        m = plugin_manifest(reports)
        assert m["loaded"] == 1
        assert m["rejected"] == 1
        assert "manifest_sha256" in m
        assert len(m["manifest_sha256"]) == 64   # sha256 hex

    def test_manifest_stable(self):
        reports = [
            {"distribution": "x", "version": "1.0", "ep_name": "ep",
             "ep_group": _GROUP_FACTS, "registered_ids": ["x.fact"],
             "digest": "d1", "status": "ok", "reject_reason": None},
        ]
        m1 = plugin_manifest(reports)
        m2 = plugin_manifest(reports)
        assert m1["manifest_sha256"] == m2["manifest_sha256"]


# ---------------------------------------------------------------------------
# 9. Direct register() helper
# ---------------------------------------------------------------------------

class TestDirectRegister:

    def test_idempotent_registration(self):
        """register() populates all descriptors; a second call raises on duplicate."""
        reg = BehaviorRegistry()
        register(reg)
        assert reg.has_fact("telecom.column.looks_normalized")
        assert reg.has_fact("telecom.column.suggests_network_id")
        assert reg.has_effect("telecom.action.tag_network_id")
        # The registry intentionally raises on duplicate third-party registrations
        # (allow_replace=False is the safe default — guards against shadowing).
        with pytest.raises(ValueError, match="Duplicate"):
            register(reg)

    def test_register_facts_entry_point_format(self):
        items = register_facts()
        assert isinstance(items, list)
        for desc, provider in items:
            assert isinstance(desc, FactDescriptor)
            assert callable(provider) or provider is None

    def test_register_actions_entry_point_format(self):
        items = register_actions()
        assert isinstance(items, list)
        for desc, handler in items:
            assert isinstance(desc, EffectDescriptor)
            assert callable(getattr(handler, "apply", None))
