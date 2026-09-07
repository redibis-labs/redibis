"""Tests for node registry and validate_spec (Phase 1)."""

from __future__ import annotations

import pytest

from redibis.agents.models import PipelineEdge, PipelineNode, PipelineSpec
from redibis.agents.registry import (
    NODE_REGISTRY,
    PortType,
    get_node,
    list_nodes,
    validate_spec,
)


def test_node_registry_lists_canonical_types():
    kinds = {n.type for n in list_nodes()}
    assert kinds == {
        "source", "sample", "profile", "contract", "mask", "publish", "gate", "batch",
    }


def test_legacy_alias_resolves_to_canonical():
    assert get_node("source_table").type == "source"
    assert get_node("profile_scan").type == "profile"
    assert get_node("approval_gate").type == "gate"


def test_validate_spec_rejects_incompatible_ports():
    prof = PipelineNode(kind="profile", label="Prof")
    bad = PipelineNode(kind="sample", label="Bad")
    spec = PipelineSpec(
        name="bad-wire",
        nodes=[prof, bad],
        edges=[
            PipelineEdge(source=prof.id, target=bad.id),
        ],
    )
    errors = validate_spec(spec)
    assert any("incompatible wiring" in e.message or "missing required input" in e.message for e in errors)


def test_validate_spec_accepts_valid_chain():
    src = PipelineNode(kind="source", label="Src", params={"table": "db.t"})
    sample = PipelineNode(kind="sample", label="Sample")
    prof = PipelineNode(kind="profile", label="Prof")
    contract = PipelineNode(
        kind="contract",
        label="Contract",
        params={"pii": True, "quality": True},
    )
    gate = PipelineNode(kind="gate", label="Gate")
    pub = PipelineNode(kind="publish", label="Pub")
    spec = PipelineSpec(
        name="good",
        nodes=[src, sample, prof, contract, gate, pub],
        edges=[
            PipelineEdge(source=src.id, target=sample.id),
            PipelineEdge(source=sample.id, target=prof.id),
            PipelineEdge(source=sample.id, target=contract.id),
            PipelineEdge(source=prof.id, target=contract.id),
            PipelineEdge(source=contract.id, target=gate.id),
            PipelineEdge(source=gate.id, target=pub.id),
        ],
    )
    assert validate_spec(spec) == []


def test_validate_spec_contract_requires_sub_flag():
    node = PipelineNode(
        kind="contract",
        label="Empty",
        params={"pii": False, "classify": False, "mask_rules": False, "quality": False},
    )
    spec = PipelineSpec(name="empty-contract", nodes=[node], edges=[])
    errors = validate_spec(spec)
    assert any("at least one" in e.message for e in errors)


def test_validate_spec_unknown_kind():
    node = PipelineNode(kind="not_a_real_node", label="X")
    spec = PipelineSpec(name="x", nodes=[node], edges=[])
    errors = validate_spec(spec)
    assert any("unknown node kind" in e.message for e in errors)


def test_profile_output_type():
    entry = NODE_REGISTRY["profile"]
    assert entry.output_ports[0].port_type == PortType.PROFILE_RESULT
