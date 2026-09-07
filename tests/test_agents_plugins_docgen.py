"""Tests for agent plugins, docgen, and codegen seam."""

from __future__ import annotations

import pytest

from redibis.agents.codegen import CodegenSafetyChain, CommercialFeatureError
from redibis.agents.docgen import document_pipeline, document_registry
from redibis.agents.models import PipelineNode, PipelineSpec
from redibis.agents.plugins import register_plugin_runner
from redibis.agents.node_registry import get_node, list_nodes, register_node, NodeSpec


def test_codegen_stub_raises():
    chain = CodegenSafetyChain()
    with pytest.raises(CommercialFeatureError):
        chain.propose("generate ranger policy", context={"table": "t"})


def test_document_pipeline():
    spec = PipelineSpec(
        name="demo",
        goal="Classify telecom tables",
        nodes=[PipelineNode(kind="classify", label="Classify")],
        edges=[],
    )
    doc = document_pipeline(spec)
    assert "demo" in doc["markdown"]
    assert doc["what"]


def test_document_registry():
    reg = document_registry()
    assert reg["count"] >= 8


def test_plugin_runner_decorator():
    @register_plugin_runner("test_plugin_node")
    def _run(params, *, table, context):
        return {"ok": True, "table": table}

    register_node(NodeSpec(kind="test_plugin_node", label="Test", tool="test.Plugin"))
    assert get_node("test_plugin_node").kind == "test_plugin_node"
