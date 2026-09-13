"""P0: packs may carry a first-class text_gateway/ section (not config/)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from redibis.config import RedibisConfig, TextGatewayConfig
from redibis.pack import apply_packs, load_pack, write_pack
from redibis.pack.config_allowlist import ALLOWED_CONFIG_TOP_LEVEL, validate_pack_config
from redibis.pack.errors import PackValidationError
from redibis.pack.stack_models import AppliedPackStack
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.text_rules import compile_layered_text_rules
from redibis.services.text_pii_service import TextPIIService


def _marker_pattern(marker: str) -> dict:
    token = f"TGWPACK{marker}"
    return {
        "pattern": rf"\b{token}\b",
        "entity_type": "SUPPORT_TICKET",
        "recognizer_group": "free_text",
        "script": "latin",
        "presidio_score": 0.95,
        "unvalidated_reason": "synthetic pack-authored marker for tests",
    }


def _write_tg_pack(
    path: Path,
    *,
    marker: str = "ONE",
    version: str = "1.0.0",
    pack_id: str = "eg-telecom-text",
    stem: str = "operator",
    extra_config: dict | None = None,
) -> Path:
    rules = {
        "exclude_terms": [f"pack-{marker.lower()}"],
        "patterns": {"add": {f"tgw_marker_{marker.lower()}": _marker_pattern(marker)}},
    }
    sections = {f"text_gateway/rules/{stem}.yaml": rules}
    if extra_config is not None:
        sections["config/redibis.yaml"] = extra_config
    manifest = {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {
            "id": pack_id,
            "version": version,
            "description": f"text gateway rules marker {marker}",
            "author": "tests",
        },
        "requires": {"redibis": ">=0"},
        "contents": {
            "text_gateway_rules": [stem],
            **({"config": True} if extra_config is not None else {}),
        },
        "mode": "overlay",
    }
    write_pack(path, manifest, sections, check_registries=False)
    return path


def test_allowlist_does_not_include_text_gateway():
    assert "text_gateway" not in ALLOWED_CONFIG_TOP_LEVEL
    with pytest.raises(PackValidationError, match="text_gateway"):
        validate_pack_config({"text_gateway": {"rules": {"exclude_terms": ["x"]}}})


def test_pack_rejects_text_gateway_inside_config(tmp_path: Path):
    path = tmp_path / "bad.rdbpack"
    with pytest.raises(PackValidationError):
        write_pack(
            path,
            {
                "apiVersion": "redibis.io/pack/v1",
                "kind": "RedibisPack",
                "metadata": {"id": "bad", "version": "1.0.0"},
                "requires": {"redibis": ">=0"},
                "contents": {"config": True},
                "mode": "overlay",
            },
            {"config/redibis.yaml": {"text_gateway": {"rules": {}}}},
            check_registries=False,
        )


def test_pack_imports_text_gateway_section(tmp_path: Path):
    archive = _write_tg_pack(tmp_path / "tg.rdbpack")
    pack = load_pack(archive)
    assert "operator" in pack.manifest.contents.text_gateway_rules
    assert "text_gateway/rules/operator.yaml" in pack.files
    stack = apply_packs(RedibisConfig.default(), [archive], include_builtin_default=False)
    assert "operator" in stack.text_gateway_rules
    assert stack.layers[0].uuid
    assert stack.layers[0].checksum


def test_from_stack_merges_pack_before_config(tmp_path: Path):
    archive = _write_tg_pack(tmp_path / "tg.rdbpack", marker="PACK")
    cfg = replace(
        RedibisConfig.default(),
        text_gateway=replace(
            TextGatewayConfig(),
            rules={
                "exclude_terms": ["from-config"],
                "patterns": {
                    "add": {"tgw_marker_cfg": _marker_pattern("CFG")},
                },
            },
        ),
    )
    stack = apply_packs(cfg, [archive], include_builtin_default=False)
    rs = RuleSetCompiler.from_stack(stack)
    assert any(s.startswith("pack:") for s in rs.rules_source)
    assert "config" in rs.rules_source
    assert "builtin" in rs.rules_source
    assert "from-config" in rs.text_rules.exclude_terms
    assert "pack-pack" in rs.text_rules.exclude_terms
    names = set(rs.patterns)
    assert "tgw_marker_pack" in names
    assert "tgw_marker_cfg" in names


def test_five_level_precedence():
    overlay, sources = compile_layered_text_rules(
        pack_docs=[{"exclude_terms": ["from-pack"]}],
        pack_source_labels=["pack:eg-telecom@1.4.0"],
        config_rules={"exclude_terms": ["from-config"]},
        persisted_rules={"exclude_terms": ["from-persisted"]},
        draft_rules={"exclude_terms": ["from-draft"]},
        include_builtin=True,
    )
    assert sources == (
        "builtin",
        "pack:eg-telecom@1.4.0",
        "config",
        "persisted",
        "draft",
    )
    terms = set(t.casefold() for t in overlay.exclude_terms)
    assert "from-pack" in terms
    assert "from-config" in terms
    assert "from-persisted" in terms
    assert "from-draft" in terms
    assert "agent" in terms  # builtin


def test_pack_changes_detection(tmp_path: Path):
    archive = _write_tg_pack(tmp_path / "tg.rdbpack", marker="HIT")
    stack = apply_packs(RedibisConfig.default(), [archive], include_builtin_default=False)
    svc = TextPIIService(
        redibis_config=RedibisConfig.default(),
        pack_stack=stack,
        skip_stored_text_rules=True,
    )
    bare = TextPIIService(
        redibis_config=RedibisConfig.default(),
        pack_stack=AppliedPackStack(config=RedibisConfig.default()),
        skip_stored_text_rules=True,
    )
    text = "please quote TGWPACKHIT on the ticket"
    packed = svc.scan(text, engines="regex", min_score=0.2)
    baseline = bare.scan(text, engines="regex", min_score=0.2)
    packed_hit = any("TGWPACKHIT" in (d.text or "") for d in packed.detections)
    base_hit = any("TGWPACKHIT" in (d.text or "") for d in baseline.detections)
    assert packed_hit
    assert not base_hit


def test_from_stack_labels_originating_layer(tmp_path: Path):
    a = _write_tg_pack(tmp_path / "a.rdbpack", marker="A", pack_id="pack-a", stem="alpha")
    b = _write_tg_pack(
        tmp_path / "b.rdbpack", marker="B", pack_id="pack-b", version="2.0.0", stem="beta"
    )
    stack = apply_packs(RedibisConfig.default(), [a, b], include_builtin_default=False)
    assert stack.text_gateway_rule_sources["alpha"].startswith("pack:pack-a@1.0.0:")
    assert stack.text_gateway_rule_sources["beta"].startswith("pack:pack-b@2.0.0:")
    rs = RuleSetCompiler.from_stack(stack)
    assert "pack:pack-a@1.0.0:alpha" in rs.rules_source
    assert "pack:pack-b@2.0.0:beta" in rs.rules_source
    assert not any(s.startswith("pack:pack-b@2.0.0:alpha") for s in rs.rules_source)
