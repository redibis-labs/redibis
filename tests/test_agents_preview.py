"""Tests for pipeline preview estimate (Phase 4 T4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redibis.agents.models import PipelineNode, PipelineSpec
from redibis.agents.preview import estimate_pipeline_scope
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
TABLE = "telecom.customers"


@pytest.fixture
def contract_store(tmp_path) -> ContractStore:
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    store.upsert(contract, table=TABLE, workflow="test", run_id="t1")
    return store


def test_preview_from_active_contract(contract_store):
    spec = PipelineSpec(
        name="preview",
        nodes=[PipelineNode(kind="source", label="Src", params={"table": TABLE})],
        edges=[],
    )
    result = estimate_pipeline_scope(spec, contract_store=contract_store)
    assert result["tables_total"] == 1
    assert result["tables"][0]["table"] == TABLE
    assert result["tables"][0]["columns"] >= 1
    assert result["tables"][0]["has_contract"] is True


def test_preview_try_on_n_limits(contract_store):
    spec = PipelineSpec(name="x", nodes=[], edges=[])
    result = estimate_pipeline_scope(
        spec,
        contract_store=contract_store,
        tables=[TABLE, "other.missing"],
        try_on_n=1,
    )
    assert result["tables_previewed"] == 1
    assert result["tables_total"] == 2
