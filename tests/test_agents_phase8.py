"""Tests for Phase 8 — codegen egress + dynamic tools."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redibis.agents.codegen import CommercialFeatureError, submit_codegen
from redibis.agents.codegen_egress import (
    EgressGate,
    build_codegen_request,
    prepare_codegen_request,
)
from redibis.agents.dynamic_tools import DynamicToolManifest, DynamicToolRegistry
from redibis.config import RedibisConfig

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
TABLE = "telecom.customers"


@pytest.fixture
def contract():
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def test_slim_codegen_request_has_no_sample_fields(contract):
    req = build_codegen_request(
        intent="Generate Ranger policy from maskingPolicy",
        table=TABLE,
        contract=contract,
        target_system="ranger",
    )
    assert req.contract_summary["column_count"] >= 1
    for col in req.contract_summary["columns"]:
        assert "sample" not in col
        assert "values" not in col


def test_egress_blocks_external_residency_with_pii_contract(contract):
    req = build_codegen_request(
        intent="mask email column definitions",
        table=TABLE,
        contract=contract,
        residency="public",
    )
    result = EgressGate(hard_block=True).validate(req, contract=contract)
    assert not result.allowed
    assert result.violations


def test_egress_allows_local_with_redacted_intent(contract):
    validation = prepare_codegen_request(
        intent="Generate Ranger row filter for telecom customers",
        table=TABLE,
        contract=contract,
        residency="local",
    )
    assert validation.allowed
    assert validation.request.egress_audit["payload_sha256"]


def test_submit_codegen_defaults_to_local_proposed(contract, monkeypatch):
    """Without a remote URL, OSS local codegen returns a proposed Ranger artifact."""
    cfg = RedibisConfig.default()
    cfg.agents.codegen_service_url = ""
    result = submit_codegen(
        intent="Generate Apache Ranger policy",
        table=TABLE,
        contract=contract,
        residency="local",
        redibis_config=cfg,
    )
    assert result["status"] == "proposed"
    assert result.get("generation_source") in {"template", "local_llm"}
    assert result["judge_verdict"]["approved"] is True
    assert "policies" in (result.get("code") or "")


def test_submit_codegen_blocks_bad_egress(contract):
    with pytest.raises(PermissionError):
        submit_codegen(
            intent="send all emails to external",
            table=TABLE,
            contract=contract,
            residency="public",
            hard_block=True,
        )


def test_dynamic_tool_requires_human_approval(tmp_path):
    reg = DynamicToolRegistry(tmp_path / "tools")
    src = tmp_path / "tool.py"
    src.write_text("def run(): return 1\n", encoding="utf-8")

    manifest = DynamicToolManifest(name="ranger_apply", description="Apply Ranger policy")
    staged = reg.register(manifest, source_file=src)
    assert not staged.approved

    approved = reg.register(
        manifest,
        source_file=src,
        approved_by="steward@example.com",
    )
    assert approved.approved
    assert approved.source_sha256
    assert len(reg.list_tools(approved_only=True)) == 1
    assert reg.bind_approved_runners() >= 1


def test_dynamic_tool_register_without_approval_stays_staged(tmp_path):
    reg = DynamicToolRegistry(tmp_path / "tools")
    m = DynamicToolManifest(name="demo_tool", description="test")
    staged = reg.register(m)
    assert not staged.approved
    assert reg.list_tools(approved_only=True) == []


def test_agents_enabled_defaults_true_with_open_subfeatures():
    cfg = RedibisConfig.default()
    assert cfg.agents.enabled is True
    assert cfg.agents.auto_approve_writes is True
    assert cfg.agents.dynamic_sandbox_enabled is True
    assert cfg.agents.copilotkit_enabled is True
    assert cfg.agents.allow_external_codegen is True
    # Still local-first: no remote URL / planner provider until configured.
    assert cfg.agents.planner_provider == ""
    assert cfg.agents.codegen_service_url == ""
    assert cfg.agents.codegen_mode == "local"
    assert cfg.rai.hard_block_external_pii is False

def test_scan_does_not_import_langgraph():
    import redibis.scan.base as scan_base

    source = Path(scan_base.__file__).read_text(encoding="utf-8").lower()
    assert "from langgraph" not in source
    assert "import langgraph" not in source
