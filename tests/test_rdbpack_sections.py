"""Phase 5 — behavior drafts, quality rulesets, masking plan templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from redibis.masking.plan import MaskingPlan
from redibis.pack import PackStackStore, PackValidationError, apply_packs, write_pack
from redibis.pack.sections import validate_masking_plan_document
from redibis.services.behavior_service import BehaviorPolicyService

MINIMAL_POLICY = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {
        "id": "pack-demo-policy",
        "version": "1.0.0",
        "description": "Demo demotion from pack import",
    },
    "appliesTo": {"engine": "pii", "stage": "post_verdict"},
    "rules": [
        {
            "id": "demote-demo",
            "when": {"fact": "column.name", "op": "eq", "value": "demo_col"},
            "effects": [
                {"effect": "core.verdict.set_detected", "params": {"detected": False}},
            ],
            "reason": "demo column is not PII",
            "priority": 5,
            "terminal": True,
        }
    ],
}

QUALITY_RULESET = {
    "name": "cdr-baseline",
    "description": "Curated GE baseline for CDR tables",
    "expectations": [
        {
            "expectation_type": "expect_column_values_to_not_be_null",
            "column": "msisdn",
            "kwargs": {},
        }
    ],
}


def _masking_template() -> dict:
    plan = MaskingPlan(
        schema_table="telecom.customers",
        name="fr-default",
        source="pack",
        columns=[],
    )
    d = plan.to_dict()
    d["seed"] = None
    d["key_refs"] = {}
    return d


def _build_section_pack(tmp_path: Path, *, policy_id: str = "pack-demo-policy") -> Path:
    policy = dict(MINIMAL_POLICY)
    policy["metadata"] = dict(policy["metadata"])
    policy["metadata"]["id"] = policy_id
    sections = {
        f"behavior/{policy_id}@1.0.0.yaml": policy,
        "quality/rulesets/cdr-baseline.yaml": QUALITY_RULESET,
        "masking/plans/fr-default.yaml": _masking_template(),
    }
    manifest = {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {"id": "section-demo", "version": "1.0.0"},
        "requires": {
            "redibis": ">=0.5,<1",
            "registries": {
                "behavior_actions": ["core.verdict.set_detected"],
            },
        },
        "contents": {
            "behavior": [f"{policy_id}@1.0.0"],
            "quality": ["cdr-baseline"],
            "masking": ["fr-default"],
        },
        "mode": "overlay",
    }
    out = tmp_path / "section-demo.rdbpack"
    write_pack(out, manifest, sections)
    return out


def test_masking_plan_rejects_seed():
    bad = _masking_template()
    bad["seed"] = "secret-seed-value"
    with pytest.raises(PackValidationError, match="forbidden"):
        validate_masking_plan_document(bad, relpath="masking/plans/x.yaml")


def test_import_creates_behavior_draft_not_active(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    policy_dir = tmp_path / "policies"
    pack_path = _build_section_pack(tmp_path, policy_id="pack-demo-policy")

    store = PackStackStore()
    report = store.import_pack(
        pack_path,
        dry_run=False,
        activate=False,
        policy_store_dir=policy_dir,
    )
    assert report["imported"] is True
    assert report["behavior"]["count"] == 1
    assert report["behavior"]["activated"] == []
    assert "cdr-baseline" in report["stashed"]["quality_rulesets"]
    assert "fr-default" in report["stashed"]["masking_plans"]

    svc = BehaviorPolicyService(base_dir=policy_dir)
    data = svc.get("pack-demo-policy", "1.0.0")
    assert (data.get("lifecycle") or {}).get("status") == "draft"
    assert (data.get("lifecycle") or {}).get("approved") is False
    assert svc.get_active("pack-demo-policy") is None


def test_import_activate_makes_policy_active(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    policy_dir = tmp_path / "policies"
    pack_path = _build_section_pack(tmp_path, policy_id="pack-activate-policy")

    store = PackStackStore()
    report = store.import_pack(
        pack_path,
        activate=True,
        policy_store_dir=policy_dir,
    )
    assert report["behavior"]["activated"]
    svc = BehaviorPolicyService(base_dir=policy_dir)
    active = svc.get_active("pack-activate-policy")
    assert active is not None
    assert active.get("version") == "1.0.0"


def test_resolve_exposes_named_quality_and_masking(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    policy_dir = tmp_path / "policies"
    pack_path = _build_section_pack(tmp_path, policy_id="pack-resolve-policy")
    store = PackStackStore()
    store.import_pack(pack_path, policy_store_dir=policy_dir)

    stack = store.resolve()
    assert "cdr-baseline" in stack.quality_rulesets
    assert stack.quality_rulesets["cdr-baseline"]["name"] == "cdr-baseline"
    assert "fr-default" in stack.masking_plans
    assert stack.masking_plans["fr-default"].get("seed") in (None, "")
    assert stack.masking_plans["fr-default"].get("key_refs") in (None, {})


def test_apply_packs_collects_section_paths(tmp_path: Path):
    from redibis.config import RedibisConfig

    pack_path = _build_section_pack(tmp_path, policy_id="pack-apply-policy")
    stack = apply_packs(
        RedibisConfig.default(),
        [pack_path],
        include_builtin_default=False,
    )
    assert any(p.startswith("behavior/") for p in stack.behavior_policy_paths)
    assert "cdr-baseline" in stack.quality_rulesets
    assert "fr-default" in stack.masking_plans


def test_dry_run_lists_section_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    pack_path = _build_section_pack(tmp_path, policy_id="pack-dry-policy")
    report = PackStackStore().import_pack(pack_path, dry_run=True)
    assert report["dry_run"] is True
    assert any("behavior/" in f for f in report["behavior_files"])
    assert any("quality/" in f for f in report["quality_files"])
    assert any("masking/" in f for f in report["masking_files"])
    assert PackStackStore().list_layers() == []


def test_cli_import_sections(tmp_path: Path, monkeypatch):
    from redibis.cli.main import main

    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    # BEHAVIOR_POLICIES_DIR is treated as the configs root; store is <root>/behavior-policies.
    monkeypatch.setenv("BEHAVIOR_POLICIES_DIR", str(tmp_path / "configs"))
    pack_path = _build_section_pack(tmp_path, policy_id="pack-cli-policy")
    rc = main(["rdbpack", "import", str(pack_path), "--json"])
    assert rc == 0
    svc = BehaviorPolicyService(base_dir=tmp_path / "configs" / "behavior-policies")
    assert svc.get("pack-cli-policy", "1.0.0")["lifecycle"]["status"] == "draft"
