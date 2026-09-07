"""Phase 3 — Ask landing UX, routing, and /api/agents/ask."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path / "configs"))
    from redibis.config import RedibisConfig
    import redibis.webapp.backend as web
    from redibis.webapp.store_accessors import clear_stores

    clear_stores()
    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.runs_dir = str(tmp_path / "agent_runs")
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    yield TestClient(web.app)
    clear_stores()


def test_root_serves_scan_console(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "app.js" in r.text
    assert "What should we do with your data?" not in r.text
    assert 'href="/agents"' not in r.text
    assert 'href="/settings"' in r.text


def test_batch_route_serves_scan_console(client):
    r = client.get("/batch")
    assert r.status_code == 200
    assert "app.js" in r.text
    assert "What should we do with your data?" not in r.text


def test_settings_route_is_canonical_configuration_page(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "app.js" in r.text
    assert "REDIBIS_SETTINGS_PAGE" in r.text
    assert "settings.css" in r.text
    assert 'href="/agents"' not in r.text
    assert 'href="/"' in r.text


@pytest.mark.parametrize("path", ["/", "/settings", "/v2", "/reports"])
def test_normal_pages_hide_agents_nav(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert 'href="/agents"' not in r.text
    assert "Agentic Ask" not in r.text


def test_review_page_serves_run_explorer(client):
    # The Evidence Review Explorer lives at /review?table=... (not linked from
    # nav, same as /agents) — it is not a "removed" page, see
    # docs/tutorials/INTRO.md and the Evidence Review Ledger design.
    r = client.get("/review")
    assert r.status_code == 200
    assert 'href="/agents"' not in r.text


def test_agents_page_still_available(client):
    r = client.get("/agents")
    assert r.status_code == 200
    assert "What should we do with your data?" in r.text
    assert 'href="/settings"' in r.text

def test_agent_runtime_explains_model_and_deterministic_capabilities(client):
    r = client.get("/api/agents/runtime")
    assert r.status_code == 200
    body = r.json()
    assert body["planner"]["method"] in {"heuristic", "llm"}
    assert body["enrichment"]["when"]
    assert "PII evidence and equations" in body["deterministic"]
    assert "rai" in body


def test_explicit_heuristic_planner_does_not_inherit_enrichment_provider(client):
    saved = client.put(
        "/api/settings/global",
        json={
            "settings": {
                "llm_defaults": {"provider": "gemini", "model": "gemini-3.5-flash"},
                "agentic_defaults": {
                    "planner_mode": "heuristic",
                    "planner_provider": "",
                    "planner_model": "",
                },
            }
        },
    )
    assert saved.status_code == 200
    planner = client.get("/api/agents/runtime").json()["planner"]
    assert planner["method"] == "heuristic"
    assert planner["provider"] == ""
    assert planner["source"] == "global_settings"


def test_runtime_settings_are_redacted(client, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIG", "/secret/deployment/config.yaml")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    r = client.get("/api/settings/runtime")
    assert r.status_code == 200
    text = r.text
    assert "/secret/deployment/config.yaml" not in text
    assert "postgresql://secret" not in text
    assert r.json()["config_path_configured"] is True


def test_legacy_global_settings_reject_secrets_and_unknown_sections(client):
    secret = client.put(
        "/api/settings/global",
        json={"settings": {"llm_defaults": {"api_key": "sk-do-not-store"}}},
    )
    assert secret.status_code == 400
    assert "sk-do-not-store" not in secret.text

    unknown = client.put(
        "/api/settings/global",
        json={"settings": {"deployment_credentials": {"password": "hidden"}}},
    )
    assert unknown.status_code == 400
    assert "hidden" not in unknown.text


def test_ask_requires_prompt(client):
    r = client.post("/api/agents/ask", json={"prompt": "   "})
    assert r.status_code == 400


def test_ask_clarification_when_ambiguous(client):
    r = client.post("/api/agents/ask", json={"prompt": "do pii quality enrich on the data"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "clarification_required"
    assert "workflow" in body["question"].lower()


def test_ask_with_clarification_starts_run(client):
    from redibis.agents.orchestrator import Orchestrator
    from redibis.agents.run_models import AgentRun, RunStatus
    from redibis.agents.batch_executor import _utc_iso

    finished = AgentRun(
        run_id="placeholder",
        name="ask",
        status=RunStatus.COMPLETED,
        pipeline={"name": "ask", "nodes": [], "edges": []},
        tables=["telecom.customers"],
        created_at=_utc_iso(),
        finished_at=_utc_iso(),
    )

    with patch.object(Orchestrator, "execute", return_value=finished):
        r = client.post(
            "/api/agents/ask",
            json={
                "prompt": "do pii quality enrich",
                "clarification": "pii telecom.customers",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "started"
    assert body["run_id"]


def test_ask_clarification_when_no_table(client):
    r = client.post("/api/agents/ask", json={"prompt": "do something with the data"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "clarification_required"
    assert "table" in body["question"].lower()


def test_ask_starts_run(client):
    from redibis.agents.orchestrator import Orchestrator
    from redibis.agents.run_models import AgentRun, RunStatus
    from redibis.agents.batch_executor import _utc_iso

    finished = AgentRun(
        run_id="placeholder",
        name="ask",
        status=RunStatus.COMPLETED,
        pipeline={"name": "ask", "nodes": [], "edges": []},
        tables=["telecom.customers"],
        created_at=_utc_iso(),
        finished_at=_utc_iso(),
    )

    with patch.object(Orchestrator, "execute", return_value=finished):
        r = client.post(
            "/api/agents/ask",
            json={"prompt": "Profile telecom.customers"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "started"
    assert body["run_id"]
    assert "telecom.customers" in body["tables"]
    assert body["planner"]["method"] in {
        "heuristic", "heuristic_fallback", "llm", "llm_repair"
    }
    assert body["planner"]["purpose"] == "intent_planning"
    assert body["pipeline"]["nodes"]

    status = client.get(f"/api/agents/runs/{body['run_id']}")
    assert status.status_code == 200
    assert status.json()["run_id"] == body["run_id"]


def test_ask_injects_saved_enrichment_defaults(client):
    from redibis.agents.orchestrator import Orchestrator
    from redibis.agents.run_models import AgentRun, RunStatus
    from redibis.agents.batch_executor import _utc_iso

    saved = client.put(
        "/api/settings/global",
        json={
            "settings": {
                "llm_defaults": {
                    "provider": "demo",
                    "model": "offline",
                    "enabled": True,
                },
                "agentic_defaults": {
                    "planner_mode": "heuristic",
                    "planner_provider": "",
                    "planner_model": "",
                },
            }
        },
    )
    assert saved.status_code == 200
    finished = AgentRun(
        run_id="placeholder",
        name="ask",
        status=RunStatus.COMPLETED,
        pipeline={"name": "ask", "nodes": [], "edges": []},
        tables=["telecom.customers"],
        created_at=_utc_iso(),
        finished_at=_utc_iso(),
    )
    with patch.object(Orchestrator, "execute", return_value=finished):
        response = client.post(
            "/api/agents/ask",
            json={"prompt": "Profile and enrich telecom.customers"},
        )
    assert response.status_code == 200
    nodes = response.json()["pipeline"]["nodes"]
    contract = next(node for node in nodes if node["kind"] == "contract")
    assert contract["params"]["enrich"] is True
    assert contract["params"]["provider"] == "demo"
    assert contract["params"]["model"] == "offline"


def test_ask_with_file_upload(client):
    from redibis.agents.orchestrator import Orchestrator
    from redibis.agents.run_models import AgentRun, RunStatus
    from redibis.agents.batch_executor import _utc_iso

    sess = client.post("/api/agents/source/samples/session")
    assert sess.status_code == 200
    session_id = sess.json()["session_id"]

    upload = client.post(
        f"/api/agents/source/upload?session_id={session_id}",
        files=[("files", ("customers.csv", b"name,email\na,b@test.com\n", "text/csv"))],
    )
    assert upload.status_code == 200

    finished = AgentRun(
        run_id="placeholder",
        name="ask",
        status=RunStatus.COMPLETED,
        pipeline={"name": "ask", "nodes": [], "edges": []},
        tables=["customers"],
        created_at=_utc_iso(),
        finished_at=_utc_iso(),
    )

    with patch.object(Orchestrator, "execute", return_value=finished):
        r = client.post(
            "/api/agents/ask",
            json={
                "prompt": "create contract",
                "source_session_id": session_id,
                "tables": ["customers"],
            },
        )
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    assert r.json()["run_id"]
