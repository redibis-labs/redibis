"""Tests for LangGraph checkpointer factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from redibis.agents.checkpointer import build_checkpointer, drop_batch_checkpointer
from redibis.agents.pipeline_executor import langgraph_available
from redibis.config import RedibisConfig


@pytest.mark.skipif(not langgraph_available(), reason="langgraph not installed")
def test_batch_memory_checkpointer_shared_by_run_id():
    cfg = RedibisConfig.default()
    a = build_checkpointer(cfg, batch_run_id="batch-a")
    b = build_checkpointer(cfg, batch_run_id="batch-a")
    c = build_checkpointer(cfg, batch_run_id="batch-b")
    assert a is b
    assert a is not c
    drop_batch_checkpointer("batch-a")
    d = build_checkpointer(cfg, batch_run_id="batch-a")
    assert d is not a


@pytest.mark.skipif(not langgraph_available(), reason="langgraph not installed")
def test_postgres_checkpointer_when_memory_enabled():
    cfg = RedibisConfig.default()
    cfg.memory.enabled = True
    cfg.memory.dsn_ref = "postgresql://user:pass@localhost/redibis"

    fake_pg = MagicMock(name="PostgresSaver")
    with patch("redibis.agents.checkpointer._postgres_saver", return_value=fake_pg):
        cp = build_checkpointer(cfg, batch_run_id="any-batch")
        assert cp is fake_pg
        cp2 = build_checkpointer(cfg, batch_run_id="other-batch")
        assert cp2 is fake_pg
