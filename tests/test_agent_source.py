"""Load sessions from the agent source root."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_agent_loader_storage")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_agent_loader_configs")


@pytest.fixture
def agent_root(tmp_path, monkeypatch):
    root = tmp_path / "agent_runs"
    root.mkdir()
    monkeypatch.setenv("AGENT_OUTPUT_DIR", str(root))
    return root


def test_load_agent_source_session(agent_root):
    from redibis.services.session import loader as load_module
    from redibis.services.session.config import GlobalConfig
    from redibis.services.session.state import ScanSession, session_manager

    session_id = "agent-run-42"
    session_dir = agent_root / session_id
    session_dir.mkdir(parents=True)
    data_path = session_dir / "data.csv"
    data_path.write_text("name\nalice\n", encoding="utf-8")

    session = ScanSession(
        session_id=session_id,
        table_name="telecom.subscribers",
        data_path=str(data_path),
        common_config=GlobalConfig(),
    )
    session.status = "scan_complete"
    session.source = "agent"
    from redibis.services.session.state import ApprovedProperty

    session.approved.add(
        ApprovedProperty(
            prop_id="ap_pii_test01",
            kind="pii",
            column="name",
            payload={"semanticType": "PERSON_NAME"},
            source="agent",
        )
    )
    session.persist_to_disk()
    session_manager.delete_session(session_id)

    result = load_module.load_session(session_id, "agent")
    assert result["source"] == "agent"
    assert result["summary"]["table_name"] == "telecom.subscribers"

    live = session_manager.get_session(session_id)
    assert live is not None
    assert live.source == "agent"
    assert len(live.approved.items) == 1
