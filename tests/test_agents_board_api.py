"""Tests for agent board API (Phase 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    from redibis.webapp.backend import app

    return TestClient(app)


def test_agents_provider_validate_rejects_gemini_without_key(client, monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    providers_file = tmp_path / "providers.json"
    providers_file.write_text(
        json.dumps(
            {
                "providers": {
                    "gemini": {
                        "description": "Google Gemini (cloud API)",
                        "litellm_model": "gemini/gemini-2.0-flash",
                        "model_prefix": "gemini",
                        "api_key_env": "GEMINI_API_KEY",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(providers_file))
    res = client.post("/api/agents/providers/validate", json={"provider": "gemini"})
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is False
    assert "GEMINI_API_KEY" in body["reason"]


def test_agents_provider_validate_accepts_gemini_with_env_key(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    res = client.post("/api/agents/providers/validate", json={"provider": "gemini"})
    assert res.status_code == 200
    assert res.json()["valid"] is True


def test_agents_registry_endpoint(client):
    res = client.get("/api/agents/registry")
    assert res.status_code == 200
    data = res.json()
    kinds = {n["type"] for n in data["nodes"]}
    assert "source" in kinds
    assert "contract" in kinds


def test_agents_nodes_returns_canonical_palette(client):
    res = client.get("/api/agents/nodes")
    assert res.status_code == 200
    kinds = {n["kind"] for n in res.json()["nodes"]}
    assert "sample" in kinds
    assert "gate" in kinds


def test_agents_validate_rejects_bad_graph(client):
    res = client.post("/api/agents/validate", json={
        "pipeline": {
            "name": "bad",
            "nodes": [{"id": "a", "kind": "profile", "label": "P"}],
            "edges": [],
        },
    })
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is False
    assert body["errors"]


def test_agents_preview_endpoint(client, tmp_path, monkeypatch):
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend
    import redibis.webapp.backend as web

    fixture = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
    store = ContractStore(LocalBackend(str(tmp_path / "c")), bucket="active-contracts")
    store.upsert(yaml.safe_load(fixture.read_text()), table="telecom.customers", workflow="t")
    monkeypatch.setattr(web, "get_contract_store", lambda: store)

    res = client.post("/api/agents/preview", json={
        "pipeline": {
            "name": "p",
            "nodes": [{"id": "1", "kind": "source", "label": "S", "params": {"table": "telecom.customers"}}],
            "edges": [],
        },
    })
    assert res.status_code == 200
    data = res.json()
    assert data["tables_total"] >= 1


def test_agents_execute_blocked_when_disabled(client, monkeypatch):
    import redibis.webapp.backend as web
    from redibis.config import RedibisConfig

    cfg = RedibisConfig.default()
    cfg.agents.enabled = False
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)

    res = client.post("/api/agents/execute", json={
        "pipeline": {"name": "x", "nodes": [], "edges": []},
        "table": "telecom.customers",
    })
    assert res.status_code == 403


def test_agents_codegen_status_endpoint(client):
    res = client.get("/api/agents/codegen/status")
    assert res.status_code == 200
    data = res.json()
    assert "service_url_configured" in data
    assert data["default_target"] == "ranger"


def test_agents_codegen_request_endpoint(client, tmp_path, monkeypatch):
    import redibis.webapp.backend as web
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    fixture = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
    store = ContractStore(LocalBackend(str(tmp_path / "c")), bucket="active-contracts")
    store.upsert(yaml.safe_load(fixture.read_text()), table="telecom.customers", workflow="t")
    monkeypatch.setattr(web, "get_contract_store", lambda: store)

    res = client.post("/api/agents/codegen/request", json={
        "intent": "Generate Ranger policy from maskingPolicy",
        "table": "telecom.customers",
        "residency": "local",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["allowed"] is True
    assert data["request"]["contract_summary"]["column_count"] >= 1

    import redibis.webapp.backend as web
    from redibis.config import RedibisConfig

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.planner_provider = ""
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    monkeypatch.setattr(
        web,
        "_global_agentic_defaults",
        lambda: {"planner_provider": "", "planner_model": ""},
    )
    monkeypatch.setattr(web, "_default_llm_provider", lambda: "")
    monkeypatch.setattr(
        "redibis.agents.run_defaults.load_defaults",
        lambda: {"planner": {}},
    )

    res = client.post("/api/agents/plan", json={"intent": "onboard tables"})
    assert res.status_code == 200
    assert res.json()["planner"]["method"] == "heuristic"
    assert res.json()["pipeline"]["nodes"]


def test_agents_defaults_endpoint_prefers_global_agentic_defaults(client, monkeypatch):
    from redibis.config import RedibisConfig
    from redibis.webapp.store_accessors import clear_stores
    import redibis.webapp.backend as web

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    clear_stores()

    saved = client.put("/api/settings/global", json={
        "settings": {
            "llm_defaults": {"provider": "gemini", "model": "gemini-3.5-flash"},
            "agentic_defaults": {"planner_provider": "claude", "planner_model": "claude-sonnet-5"},
        }
    })
    assert saved.status_code == 200

    res = client.get("/api/agents/defaults")
    assert res.status_code == 200
    planner = res.json()["defaults"]["planner"]
    assert planner["provider"] == "claude"
    assert planner["model"] == "claude-sonnet-5"


def test_agents_plan_uses_global_agentic_defaults_when_provider_missing(client, monkeypatch):
    from types import SimpleNamespace

    from redibis.agents.models import PipelineSpec
    from redibis.config import RedibisConfig
    from redibis.webapp.store_accessors import clear_stores
    import redibis.webapp.backend as web

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    clear_stores()

    saved = client.put("/api/settings/global", json={
        "settings": {
            "agentic_defaults": {"planner_provider": "gemini", "planner_model": "gemini-3.5-flash"},
        }
    })
    assert saved.status_code == 200

    seen: dict[str, str] = {}

    def _fake_get_provider(name, model="", **kwargs):
        seen["provider"] = name
        seen["model"] = model
        return SimpleNamespace(name=name, model=model)

    class _FakePlanner:
        def __init__(self, provider, redibis_config):
            self.provider = provider

        def plan(self, intent, context):
            return SimpleNamespace(
                valid=True,
                errors=[],
                repaired=False,
                spec=PipelineSpec(name="planned", nodes=[], edges=[]),
                rai_report=None,
            )

    monkeypatch.setattr("redibis.enrich.providers.get_provider", _fake_get_provider)
    monkeypatch.setattr("redibis.agents.planner.IntentPlanner", _FakePlanner)

    res = client.post("/api/agents/plan", json={"intent": "onboard telecom tables"})
    assert res.status_code == 200
    assert seen["provider"] == "gemini"
    assert seen["model"] == "gemini-3.5-flash"
    assert res.json()["valid"] is True


def test_agents_batch_resume_api_continues_hitl_run(client, tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    import copy

    from redibis.agents import BatchExecutor, LineageStore, PipelineSpec, ToolContext
    from redibis.agents.models import PipelineNode
    from redibis.agents.pipeline_executor import langgraph_available
    from redibis.agents.run_models import RunStatus
    from redibis.config import RedibisConfig
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend
    import redibis.webapp.backend as web

    if not langgraph_available():
        pytest.skip("langgraph not installed")

    fixture = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
    contract = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    store = ContractStore(LocalBackend(str(tmp_path / "c")), bucket="active-contracts")
    store.upsert(contract, table="telecom.customers", workflow="t")
    store.upsert(copy.deepcopy(contract), table="telecom.subscribers", workflow="t")
    monkeypatch.setattr(web, "get_contract_store", lambda: store)

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.runs_dir = str(tmp_path / "agent_runs")
    cfg.agents.batch_executor = "langgraph"
    cfg.agents.auto_approve_writes = False
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)

    gate = PipelineNode(kind="gate", label="Steward gate", params={"role": "Steward"})
    spec = PipelineSpec(name="hitl-batch-api", nodes=[gate], edges=[])
    ctx = ToolContext(
        contract_store=store,
        config=cfg,
        sub_store=MagicMock(),
    )
    lineage = LineageStore(tmp_path / "agent_runs")
    executor = BatchExecutor(lineage, tool_ctx=ctx)

    with patch("redibis.agents.executor.validate_spec", return_value=[]):
        run = executor.run(spec, tables=["telecom.customers", "telecom.subscribers"])

    assert run.status == RunStatus.AWAITING_HITL

    res = client.post(
        f"/api/agents/runs/{run.run_id}/resume",
        json={
            "table": "telecom.customers",
            "approved": True,
            "approved_by": "api-test",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == RunStatus.AWAITING_HITL.value
    assert body["batch_meta"]["paused_table"] == "telecom.subscribers"

    res2 = client.post(
        f"/api/agents/runs/{run.run_id}/resume",
        json={
            "table": "telecom.subscribers",
            "approved": True,
            "approved_by": "api-test",
        },
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == RunStatus.COMPLETED.value


def test_agents_run_artifacts_list_and_download(client, tmp_path, monkeypatch):
    from redibis.agents.lineage_store import LineageStore
    from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus
    from redibis.config import RedibisConfig
    from redibis.webapp.store_accessors import clear_stores
    import redibis.webapp.backend as web

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.runs_dir = str(tmp_path / "agent_runs")
    cfg.report.output_dir = tmp_path / "scan_output"
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_output"))
    clear_stores()

    run = AgentRun(
        run_id="agent-artifacts-1",
        name="artifact-test",
        status=RunStatus.COMPLETED,
        tables=["telecom.customers"],
    )
    run.steps.append(
        StepRecord(
            step_id="step-contract",
            node_id="n1",
            node_kind="contract",
            tool="Scan.contract",
            table="telecom.customers",
            status=StepStatus.COMPLETED,
            output={
                "run_id": "20260101_120000",
                "sub_steps": [{"kind": "enrich", "valid": True}],
            },
        )
    )
    LineageStore(Path(cfg.agents.runs_dir)).save(run)

    run_dir = Path(cfg.report.output_dir) / "20260101_120000"
    (run_dir / "ge_report").mkdir(parents=True)
    (run_dir / "deep_scan_bundle").mkdir(parents=True)
    (run_dir / "evidence").mkdir(parents=True)
    (run_dir / "masked").mkdir(parents=True)
    (run_dir / "interactive_review.html").write_text("<html>profile</html>", encoding="utf-8")
    (run_dir / "triage_report.html").write_text("<html>triage</html>", encoding="utf-8")
    (run_dir / "quality-report-20260101_120000.html").write_text("<html>quality</html>", encoding="utf-8")
    (run_dir / "quality_contract.yaml").write_text("kind: DataContract\n", encoding="utf-8")
    (run_dir / "pii_contract.yaml").write_text("kind: DataContract\n", encoding="utf-8")
    (run_dir / "pii_report.json").write_text('{"pii": true}\n', encoding="utf-8")
    (run_dir / "pii_detections.html").write_text("<html>pii</html>", encoding="utf-8")
    (run_dir / "deep_scan_manifest.json").write_text('{"ok": true}\n', encoding="utf-8")
    (run_dir / "deep_scan_bundle" / "manifest.json").write_text('{"bundle": true}\n', encoding="utf-8")
    (run_dir / "deep_scan_bundle" / "contract_synthesis.md").write_text("# synthesis\n", encoding="utf-8")
    (run_dir / "deep_scan_bundle" / "columns.jsonl").write_text('{"column":"email"}\n', encoding="utf-8")
    (run_dir / "evidence" / "raw.json").write_text('{"raw": true}\n', encoding="utf-8")
    (run_dir / "masked" / "telecom_customers_mask_20260101.csv").write_text("email\nx@y.com\n", encoding="utf-8")
    (run_dir / "masked" / "telecom_customers_mask_20260101.manifest.json").write_text('{"manifest": true}\n', encoding="utf-8")
    (run_dir / "masked" / "telecom_customers_mask_20260101.audit.json").write_text('{"audit": true}\n', encoding="utf-8")
    (run_dir / "ge_report" / "index.html").write_text("<html>ge docs</html>", encoding="utf-8")
    (run_dir / "ge_report" / "site.css").write_text("body{}", encoding="utf-8")

    storage_root = tmp_path / "storage"
    enrich_dir = storage_root / "pii-reports" / "enrich" / "telecom_customers" / "20260101_120000"
    enrich_dir.mkdir(parents=True)
    (enrich_dir / "contract.deterministic.yaml").write_text("kind: DataContract\nsource: deterministic\n", encoding="utf-8")
    (enrich_dir / "contract.llm.yaml").write_text("kind: DataContract\nsource: llm\n", encoding="utf-8")
    (enrich_dir / "contract_diff.md").write_text("# Contract diff\n", encoding="utf-8")
    (enrich_dir / "contract_diff.json").write_text('{"summary": {"adds": 1}}\n', encoding="utf-8")
    (enrich_dir / "llm_prompt_context.json").write_text('{"prompt_chars": 123}\n', encoding="utf-8")
    (enrich_dir / "contract.active.ref.json").write_text('{"active_version": "v2"}\n', encoding="utf-8")

    res = client.get("/api/agents/runs/agent-artifacts-1/artifacts")
    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == "agent-artifacts-1"

    artifacts = body["artifacts"]
    names = {item["name"] for item in artifacts}
    assert "interactive_review.html" in names
    assert "triage_report.html" in names
    assert "quality-report-20260101_120000.html" in names
    assert "quality_contract.yaml" in names
    assert "pii_contract.yaml" in names
    assert "pii_report.json" in names
    assert "pii_detections.html" in names
    assert "ge_report/" in names
    assert "deep_scan_bundle/manifest.json" in names
    assert "deep_scan_bundle/contract_synthesis.md" in names
    assert "deep_scan_bundle/columns.jsonl" in names
    assert "masked/telecom_customers_mask_20260101.csv" in names
    assert "masked/telecom_customers_mask_20260101.manifest.json" in names
    assert "masked/telecom_customers_mask_20260101.audit.json" in names
    assert "contract.deterministic.yaml" in names
    assert "contract.llm.yaml" in names
    assert "contract_diff.md" in names
    assert "contract_diff.json" in names
    assert "llm_prompt_context.json" in names
    assert "deep_scan_manifest.json" not in names
    assert "evidence/raw.json" not in names
    assert "contract.active.ref.json" not in names
    assert "ge_report/index.html" not in names
    assert all("local_path" not in item and "storage_key" not in item for item in artifacts)

    diff_artifact = next(item for item in artifacts if item["name"] == "contract_diff.md")
    diff_download = client.get(
        f"/api/agents/runs/agent-artifacts-1/artifacts/{diff_artifact['id']}"
    )
    assert diff_download.status_code == 200
    assert diff_download.text == "# Contract diff\n"

    ge_artifact = next(item for item in artifacts if item["name"] == "ge_report/")
    ge_download = client.get(
        f"/api/agents/runs/agent-artifacts-1/artifacts/{ge_artifact['id']}"
    )
    assert ge_download.status_code == 200
    assert ge_download.headers["content-type"].startswith("application/zip")
    assert ge_download.content[:2] == b"PK"


def test_agents_run_artifacts_ignores_explicit_paths_outside_run_scope(client, tmp_path, monkeypatch):
    from redibis.agents.lineage_store import LineageStore
    from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus
    from redibis.config import RedibisConfig
    from redibis.webapp.store_accessors import clear_stores
    import redibis.webapp.backend as web

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.runs_dir = str(tmp_path / "agent_runs")
    cfg.report.output_dir = tmp_path / "scan_output"
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_output"))
    clear_stores()

    outside = tmp_path / "outside-secret.html"
    outside.write_text("<html>secret</html>", encoding="utf-8")
    run_dir = Path(cfg.report.output_dir) / "20260101_130000"
    run_dir.mkdir(parents=True)
    (run_dir / "triage_report.html").write_text("<html>ok</html>", encoding="utf-8")

    run = AgentRun(
        run_id="agent-artifacts-2",
        name="artifact-guard-test",
        status=RunStatus.COMPLETED,
        tables=["telecom.customers"],
    )
    run.steps.append(
        StepRecord(
            step_id="step-pii",
            node_id="n2",
            node_kind="pii_scan",
            tool="Scan.detect_pii",
            table="telecom.customers",
            status=StepStatus.COMPLETED,
            output={
                "run_id": "20260101_130000",
                "artifacts": {"evil": str(outside)},
            },
        )
    )
    LineageStore(Path(cfg.agents.runs_dir)).save(run)

    res = client.get("/api/agents/runs/agent-artifacts-2/artifacts")
    assert res.status_code == 200
    names = {item["name"] for item in res.json()["artifacts"]}
    assert "triage_report.html" in names
    assert "outside-secret.html" not in names


def test_agents_run_debug_and_task_log_artifacts(client, tmp_path, monkeypatch):
    from redibis.agents.lineage_store import LineageStore
    from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus
    from redibis.config import RedibisConfig
    from redibis.webapp.store_accessors import clear_stores
    import redibis.webapp.backend as web

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.runs_dir = str(tmp_path / "agent_runs")
    cfg.report.output_dir = tmp_path / "scan_output"
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_output"))
    clear_stores()

    scan_run_id = "20260101_140000"
    run_dir = Path(cfg.report.output_dir) / scan_run_id
    run_dir.mkdir(parents=True)
    (run_dir / "triage_report.html").write_text("<html>ok</html>", encoding="utf-8")
    (run_dir / "telecom.customers.20260101_140000.log").write_text(
        "DECISION column=email detected=true\n",
        encoding="utf-8",
    )

    run = AgentRun(
        run_id="agent-debug-artifacts",
        name="debug-artifact-test",
        status=RunStatus.FAILED,
        tables=["telecom.customers"],
        error="step blew up",
        logs=["telecom.customers · contract — failed"],
    )
    run.steps.append(
        StepRecord(
            step_id="step-fail",
            node_id="n1",
            node_kind="contract",
            tool="Scan.contract",
            table="telecom.customers",
            status=StepStatus.FAILED,
            error="boom",
            output={"run_id": scan_run_id},
        )
    )
    store = LineageStore(Path(cfg.agents.runs_dir))
    store.save(run)

    agent_dir = Path(cfg.agents.runs_dir) / run.run_id
    assert (agent_dir / "run_trace.json").is_file()
    assert (agent_dir / "run_debug.log").is_file()

    res = client.get("/api/agents/runs/agent-debug-artifacts/artifacts")
    assert res.status_code == 200
    artifacts = res.json()["artifacts"]
    names = {item["name"] for item in artifacts}
    kinds = {item["name"]: item["kind"] for item in artifacts}
    assert "run_trace.json" in names
    assert "run_debug.log" in names
    assert kinds["run_debug.log"] == "log"
    assert "telecom.customers.debug.log" in names
    assert kinds["telecom.customers.debug.log"] == "log"

    debug_art = next(item for item in artifacts if item["name"] == "run_debug.log")
    dl = client.get(f"/api/agents/runs/agent-debug-artifacts/artifacts/{debug_art['id']}")
    assert dl.status_code == 200
    assert "DECISION column=email" in dl.text
    assert "step blew up" in dl.text


def test_agents_run_artifacts_include_active_contract(client, tmp_path, monkeypatch):
    """Ask/Results downloads must expose the active ODCS contract, not only logs."""
    from redibis.agents.lineage_store import LineageStore
    from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus
    from redibis.config import RedibisConfig
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend
    import redibis.webapp.backend as web
    from redibis.webapp.store_accessors import clear_stores

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.runs_dir = str(tmp_path / "agent_runs")
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    clear_stores()

    contract_store = ContractStore(LocalBackend(tmp_path / "contracts"), bucket="active-contracts")
    contract_store.upsert(
        {
            "apiVersion": "v3.0.1",
            "kind": "DataContract",
            "name": "customers_contract",
            "version": "1.0.0",
            "schema": [{
                "name": "customers",
                "physicalName": "telecom.customers",
                "properties": [{"name": "email", "logicalType": "string"}],
            }],
        },
        table="telecom.customers",
        workflow="manual",
    )
    monkeypatch.setattr(web, "get_contract_store", lambda: contract_store)
    monkeypatch.setattr("redibis.webapp.backend.get_contract_store", lambda: contract_store)

    run = AgentRun(
        run_id="ask-contract-dl",
        name="ask",
        status=RunStatus.COMPLETED,
        pipeline={"name": "ask", "nodes": [], "edges": []},
        tables=["telecom.customers"],
        steps=[
            StepRecord(
                step_id="s1",
                node_id="e1",
                node_kind="enrich",
                tool="enrich",
                table="telecom.customers",
                status=StepStatus.COMPLETED,
                output={
                    "run_id": "enrich_run_1",
                    "auto_written": True,
                    "valid": True,
                    "table": "telecom.customers",
                },
            ),
        ],
    )
    LineageStore(Path(cfg.agents.runs_dir)).save(run)

    res = client.get("/api/agents/runs/ask-contract-dl/artifacts")
    assert res.status_code == 200
    artifacts = res.json()["artifacts"]
    names = {item["name"] for item in artifacts}
    assert "contract.active.telecom_customers.yaml" in names
    contract_art = next(
        item for item in artifacts if item["name"] == "contract.active.telecom_customers.yaml"
    )
    assert contract_art["kind"] == "contract"
    dl = client.get(f"/api/agents/runs/ask-contract-dl/artifacts/{contract_art['id']}")
    assert dl.status_code == 200
    assert "customers_contract" in dl.text
    assert "email" in dl.text
