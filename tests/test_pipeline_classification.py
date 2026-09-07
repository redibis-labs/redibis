"""Phase 0 — classification pipeline step + RAI guarded_model_call e2e."""

from __future__ import annotations

import yaml
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from redibis.config import RedibisConfig
from redibis.services import pipeline
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.telemetry.model_gateway import guarded_model_call

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
TABLE = "telecom.customers"


@pytest.fixture
def contract_store(tmp_path) -> ContractStore:
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    store.upsert(contract, table=TABLE, workflow="test", run_id="t1")
    return store


def test_pipeline_run_classification_when_enabled(contract_store):
    contract = contract_store.get_active(TABLE)
    cfg = RedibisConfig()
    cfg.classification.enabled = True
    cfg.classification.default_jurisdiction = "EU"

    rows = pipeline.run_classification(contract, TABLE, config=cfg)
    assert rows
    columns = {row["column"] for row in rows}
    assert columns


def test_pipeline_run_classification_disabled_returns_empty(contract_store):
    contract = contract_store.get_active(TABLE)
    cfg = RedibisConfig()
    cfg.classification.enabled = False

    assert pipeline.run_classification(contract, TABLE, config=cfg) == []


def test_guarded_model_call_blocks_external_residency_with_pii_contract(contract_store):
    """E2E: PII-tagged contract + external residency → hard block before fn runs."""
    contract = contract_store.get_active(TABLE)
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = True
    cfg.rai.enforce = False
    cfg.rai.mode = "report"

    calls: list[int] = []

    def _would_call_model() -> str:
        calls.append(1)
        return "must-not-run"

    with pytest.raises(PermissionError, match="raw PII blocked"):
        guarded_model_call(
            _would_call_model,
            model_id="public-gpt-4",
            contract=contract,
            table=TABLE,
            user_prompt="enrich column definitions",
            residency="public",
            redibis_config=cfg,
        )

    assert calls == []


def test_guarded_model_call_allows_local_residency_with_pii_contract(contract_store):
    contract = contract_store.get_active(TABLE)
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = True

    result, report = guarded_model_call(
        lambda: "ok",
        model_id="local-llm",
        contract=contract,
        table=TABLE,
        residency="local",
        redibis_config=cfg,
    )
    assert result == "ok"
    assert report is not None
    assert report["contains_raw_pii"] is True
    assert report["residency"] == "local"
