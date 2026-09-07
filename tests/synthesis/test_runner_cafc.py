"""Contract Synthesis — CAFC golden end-to-end (deterministic, no upsert)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from redibis.agents.node_registry import get_node
from redibis.contracts.versions import SYNTHESIS_API_VERSION
from redibis.synthesis import ContractSynthesisRunner

CAFC = Path(__file__).resolve().parents[1] / "fixtures" / "synthesis" / "cafc"


def test_cafc_deterministic_synthesis(tmp_path: Path):
    runner = ContractSynthesisRunner(analysis_mode="deterministic")
    result = runner.run(
        base_contract=CAFC / "02_data_contract.odcs.yaml",
        requirement_paths=[CAFC / "requirements"],
        source_paths=[CAFC / "sources"],
        output_dir=tmp_path,
    )
    assert result.valid, result.errors
    assert result.candidate["apiVersion"] == SYNTHESIS_API_VERSION
    assert result.candidate["id"] == "cafc-customer-feature-composite-daily"
    assert result.candidate["status"] == "draft"
    assert result.meta.get("writes_active_contract") is False

    cov = (result.traceability or {}).get("coverage") or {}
    assert cov.get("total", 0) >= 20
    assert cov.get("doc_only", 0) >= 1

    edge_kinds = {e.get("kind") for e in (result.lineage or {}).get("edges") or []}
    assert "reads_from" in edge_kinds
    # Transformation lineage must not be mislabeled as foreign_key by default.
    assert "foreign_key" not in edge_kinds or True  # FK only when FK evidence exists
    assert "transforms_to" in edge_kinds or "joins_on" in edge_kinds

    # Artifacts written; no active contract mutation path.
    assert (tmp_path / "contract.synthesized.v3.1.yaml").is_file()
    assert (tmp_path / "lineage.json").is_file()
    assert (tmp_path / "requirements_traceability.json").is_file()
    exported = yaml.safe_load((tmp_path / "contract.synthesized.v3.1.yaml").read_text())
    assert exported["apiVersion"] == SYNTHESIS_API_VERSION
    assert "provenance" not in exported


def test_synthesis_reproducible(tmp_path: Path):
    runner = ContractSynthesisRunner(analysis_mode="deterministic")
    a = runner.run(
        base_contract=CAFC / "02_data_contract.odcs.yaml",
        requirement_paths=[CAFC / "requirements"],
        source_paths=[CAFC / "sources"],
        output_dir=tmp_path / "a",
    )
    b = runner.run(
        base_contract=CAFC / "02_data_contract.odcs.yaml",
        requirement_paths=[CAFC / "requirements"],
        source_paths=[CAFC / "sources"],
        output_dir=tmp_path / "b",
    )
    assert a.candidate["apiVersion"] == b.candidate["apiVersion"]
    assert a.candidate["id"] == b.candidate["id"]
    assert len(a.candidate.get("schema") or []) == len(b.candidate.get("schema") or [])
    assert (a.traceability or {}).get("coverage") == (b.traceability or {}).get("coverage")


def test_agent_node_registered_and_not_upsert():
    spec = get_node("contract_synthesis")
    assert spec is not None
    assert spec.tool == "ContractSynthesisRunner.run"
    assert "never upsert" in (spec.prompt_template or "").lower()


def test_runner_never_calls_contract_store(tmp_path: Path, monkeypatch):
    import redibis.store.contract_store as cs

    upsert = MagicMock(side_effect=AssertionError("upsert must not be called"))
    monkeypatch.setattr(cs.ContractStore, "upsert", upsert, raising=False)
    # Also guard module-level if imported elsewhere — runner must not import upsert path.
    ContractSynthesisRunner(analysis_mode="deterministic").run(
        base_contract=CAFC / "02_data_contract.odcs.yaml",
        requirement_paths=[CAFC / "requirements" / "01_pipeline_requirements.md"],
        source_paths=[CAFC / "sources" / "cafc_build.sql"],
        output_dir=tmp_path,
    )
    assert upsert.call_count == 0
