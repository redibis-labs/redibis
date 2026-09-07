"""Tests for PII LLM refiner and DAG trace step nodes."""

from __future__ import annotations

import json

import pytest

from redibis.agents.dag_trace import run_to_react_flow
from redibis.agents.models import PipelineEdge, PipelineNode, PipelineSpec
from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus
from redibis.config import RedibisConfig
from redibis.enrich.providers import EnrichmentProvider
from redibis.models import PIIDetection
from redibis.pii.llm_refiner import (
    needs_llm_refinement,
    refine_detections,
    refine_with_llm,
)
from redibis.pii.thresholds import Thresholds


class FakeRefinerProvider(EnrichmentProvider):
    def __init__(self, response: dict):
        super().__init__(model="fake-refiner", residency="local")
        self.name = "fake-refiner"
        self._response = response
        self.prompts: list[str] = []

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        self.prompts.append(user_prompt)
        assert "29001011401234" not in user_prompt
        return json.dumps(self._response)


def test_needs_llm_refinement_ambiguous_band():
    det = PIIDetection(column="email", detected=False, presidio_score=0.55)
    assert needs_llm_refinement(det)


def test_needs_llm_refinement_engine_disagreement():
    det = PIIDetection(
        column="email",
        detected=False,
        presidio_score=0.9,
        gliner_score=0.2,
    )
    assert needs_llm_refinement(det)


def test_refine_with_llm_uses_shapes_not_raw_values():
    provider = FakeRefinerProvider({
        "verdict": "PII",
        "confidence": 0.88,
        "entity_type": "EMAIL_ADDRESS",
        "reasoning": "shape looks like email",
    })
    result = refine_with_llm(
        column="email",
        sample_values=["alice@example.com"],
        current_entity="UNKNOWN",
        current_confidence=0.5,
        presidio_score=0.55,
        provider=provider,
        redibis_config=RedibisConfig(),
    )
    assert result["verdict"] == "PII"
    assert provider.prompts
    assert "alice@example.com" not in provider.prompts[0]


def test_refine_detections_populates_llm_fields():
    provider = FakeRefinerProvider({
        "verdict": "NOT_PII",
        "confidence": 0.91,
        "entity_type": "UNKNOWN",
        "reasoning": "status code",
    })
    cfg = RedibisConfig()
    cfg.pii.llm.enabled = True
    detections = [
        PIIDetection(column="status", detected=False, presidio_score=0.5),
    ]
    out = refine_detections(detections, Thresholds(), redibis_config=cfg, provider=provider)
    assert out[0].llm_score == 0.91
    assert out[0].llm_verdict == "REJECTED"


def test_dag_trace_includes_step_child_nodes():
    spec = PipelineSpec(
        name="t",
        nodes=[
            PipelineNode(id="n1", kind="profile_scan", label="Profile", position={"x": 0, "y": 0}),
        ],
        edges=[],
    )
    run = AgentRun(
        name="t",
        status=RunStatus.COMPLETED,
        pipeline=spec.to_dict(),
        tables=["db.t1", "db.t2"],
        steps=[
            StepRecord(
                step_id="a",
                node_id="n1",
                node_kind="profile_scan",
                tool="Scan.profile",
                table="db.t1",
                status=StepStatus.COMPLETED,
            ),
            StepRecord(
                step_id="b",
                node_id="n1",
                node_kind="profile_scan",
                tool="Scan.profile",
                table="db.t2",
                status=StepStatus.FAILED,
                error="boom",
            ),
        ],
    )
    trace = run_to_react_flow(run)
    steps = [n for n in trace["nodes"] if n["data"].get("kind") == "trace_step"]
    assert len(steps) == 2
    assert steps[0]["position"]["y"] != steps[1]["position"]["y"]
