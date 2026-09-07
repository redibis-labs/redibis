"""Tests for bounded dynamic-tool sandbox execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from redibis.agents.dynamic_sandbox import SandboxError, run_dynamic_tool, validate_dynamic_source
from redibis.agents.dynamic_tools import DynamicToolManifest, DynamicToolRegistry
from redibis.agents.models import PipelineNode
from redibis.agents.tool_runner import ToolContext, run_node_step


def test_validate_rejects_imports():
    errs = validate_dynamic_source("import os\ndef run(p, *, table, context): return {}")
    assert errs
    assert any("Import" in e for e in errs)


def test_validate_rejects_eval():
    errs = validate_dynamic_source("def run(p, *, table, context):\n    eval('1')")
    assert errs
    assert any("eval" in e for e in errs)


def test_sandbox_runs_simple_tool(tmp_path):
    src = tmp_path / "tool.py"
    src.write_text(
        "def run(params, *, table, context):\n"
        "    return {'ok': True, 'tag': params.get('tag', ''), 'table': table}\n",
        encoding="utf-8",
    )
    manifest = DynamicToolManifest(
        name="demo",
        source_path=str(src),
        source_sha256=DynamicToolRegistry.hash_source(src),
        approved=True,
        policy_scope=["metadata"],
    )
    out = run_dynamic_tool(src, manifest, {"tag": "x"}, table="db.t", context=ToolContext())
    assert out["ok"] is True
    assert out["sandbox"] is True
    assert out["table"] == "db.t"


def test_sandbox_rejects_hash_mismatch(tmp_path):
    src = tmp_path / "tool.py"
    src.write_text("def run(p, *, table, context): return {}\n", encoding="utf-8")
    manifest = DynamicToolManifest(name="demo", source_sha256="deadbeef", approved=True)
    with pytest.raises(SandboxError, match="hash mismatch"):
        run_dynamic_tool(src, manifest, {}, table="db.t", context=ToolContext())


def test_registry_sandbox_runner_executes(tmp_path):
    reg = DynamicToolRegistry(tmp_path / "tools")
    src = tmp_path / "tool.py"
    src.write_text(
        "def run(params, *, table, context):\n"
        "    return {'count': len(params), 'dry_run': context.dry_run}\n",
        encoding="utf-8",
    )
    manifest = DynamicToolManifest(name="counter", description="count params")
    reg.register(manifest, source_file=src, approved_by="steward@test")
    reg.bind_approved_runners(sandbox_enabled=True)

    node = PipelineNode(kind="dynamic:counter", label="Counter", params={"a": 1})
    step = run_node_step(node, table="db.t", ctx=ToolContext(dry_run=True))
    assert step.status.value == "completed"
    assert step.output["count"] == 1
    assert step.output["dry_run"] is True


def test_registry_stub_when_sandbox_disabled(tmp_path):
    reg = DynamicToolRegistry(tmp_path / "tools")
    src = tmp_path / "tool.py"
    src.write_text("def run(p, *, table, context): return {'ok': True}\n", encoding="utf-8")
    manifest = DynamicToolManifest(name="demo", description="demo")
    reg.register(manifest, source_file=src, approved_by="steward@test")
    reg.bind_approved_runners(sandbox_enabled=False)

    node = PipelineNode(kind="dynamic:demo", label="Demo")
    step = run_node_step(node, table="db.t", ctx=ToolContext())
    assert step.status.value == "skipped"
    assert "dynamic_sandbox_enabled" in step.output.get("reason", "")
