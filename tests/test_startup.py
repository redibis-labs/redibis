"""Startup smoke tests for deployment validate (pytest only; no [ge]/[web] required)."""

import os
import sys
from pathlib import Path

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ["LOCAL_STORAGE_ROOT"] = "/tmp/redibis_test_storage"
os.environ["SCAN_OUTPUT_DIR"] = "/tmp/redibis_test_output"
os.environ["CONFIGS_DIR"] = "/tmp/redibis_test_configs"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

_APPROVED_SYMBOLS = (
    "ApprovedProperty",
    "ApprovedSet",
    "build_approved_partials",
    "merge_approved",
    "load_session_from_dir",
    "pii_row_to_fragment",
    "quality_row_to_fragment",
)


def test_backend_app_initialization():
    """FastAPI app loads and registers core routes."""
    pytest.importorskip("fastapi")
    from redibis.webapp.backend import app

    routes = {route.path for route in app.routes}
    assert len(app.routes) >= 216, (
        f"backend route registry too small ({len(app.routes)}); "
        "file may be truncated"
    )
    assert "/health" in routes, "/health endpoint missing from route registry"
    assert "/ready" in routes, "/ready endpoint missing from route registry"
    assert "/api/models" in routes, "/api/models endpoint missing from route registry"
    assert "/" in routes, "root dashboard route missing from route registry"
    assert "/batch" in routes, "batch scan console route missing"
    assert "/agents" in routes, "agent board route missing"
    assert "/api/agents/ask" in routes, "agent ask API missing"
    assert "/api/agents/nodes" in routes, "agent nodes API missing"
    assert "/api/agents/registry" in routes, "agent registry API missing"
    assert "/api/agents/validate" in routes, "agent validate API missing"
    assert "/api/agents/preview" in routes, "agent preview API missing"
    assert "/api/agents/handoff" in routes, "agent handoff API missing"
    assert "/api/agents/runs/{run_id}/resume" in routes, "agent resume API missing"
    assert "/api/agents/codegen/request" in routes, "agent codegen request API missing"
    assert "/api/agents/codegen/status" in routes, "agent codegen status API missing"
    assert "/api/enterprise/status" in routes, "enterprise status API missing"
    assert "/api/agents/dynamic-tools" in routes, "agent dynamic tools API missing"
    assert "/api/agents/dashboard" in routes, "agent dashboard API missing"
    assert "/api/agents/source/upload" in routes, "agent source upload API missing"
    assert "/api/agents/source/samples" in routes, "agent source samples API missing"
    assert "/api/agents/defaults" in routes, "agent defaults pack API missing"
    assert "/api/config/rai" in routes, "global RAI config API missing"


def test_agents_open_core_imports():
    """Agent governance modules import on minimal install."""
    from redibis.agents import (
        BatchExecutor,
        PipelineSpec,
        compile_prompt_plan,
        document_pipeline,
    )
    from redibis.agents.codegen import CodegenSafetyChain, CommercialFeatureError
    from redibis.classification import ClassificationService, get_builtin_pack

    chain = CodegenSafetyChain()
    with pytest.raises(CommercialFeatureError):
        chain.propose("x", context={})

    spec = PipelineSpec(name="smoke", nodes=[], edges=[])
    doc = document_pipeline(spec)
    assert "markdown" in doc
    assert get_builtin_pack("telecom").name == "telecom"


def test_approved_module_imports():
    """Approved-basket API imports without optional great_expectations ([ge] extra)."""
    try:
        import redibis.services.session_service as session_service
    except ImportError as exc:
        if "great_expectations" in str(exc):
            pytest.fail(
                "session_service must import on a minimal install (no redibis[ge]). "
                "QualityProfiler should defer great_expectations to runtime. "
                f"ImportError: {exc}"
            )
        raise

    for name in _APPROVED_SYMBOLS:
        assert hasattr(session_service, name), f"missing symbol: {name}"

    assert callable(session_service.build_approved_partials)
    assert callable(session_service.merge_approved)
    assert callable(session_service.load_session_from_dir)

    import redibis.cli.tools as cli_tools
    assert callable(cli_tools.cmd_approved)
