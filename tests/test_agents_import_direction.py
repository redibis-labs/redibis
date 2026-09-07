"""Definition-of-done import-direction checks (handover §5.1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Deterministic spine must not import LangGraph/CopilotKit.
DETERMINISTIC_PACKAGES = (
    "redibis/scan",
    "redibis/services",
    "redibis/store",
    "redibis/quality",
    "redibis/pii",
    "redibis/classification",
    "redibis/contracts",
    "redibis/profiling",
    "redibis/masking",
)

_LANGGRAPH_IMPORT = re.compile(r"^\s*(from\s+langgraph|import\s+langgraph)", re.MULTILINE)
_LANGGRAPH_ALLOWED = {
    ROOT / "redibis/agents/executor.py",
    ROOT / "redibis/agents/copilotkit_bridge.py",
    ROOT / "redibis/agents/checkpointer.py",
    ROOT / "redibis/agents/pipeline_executor.py",
}


@pytest.mark.parametrize("rel", DETERMINISTIC_PACKAGES)
def test_deterministic_packages_avoid_langgraph(rel: str):
    base = ROOT / rel
    for path in base.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not _LANGGRAPH_IMPORT.search(text), f"{path} imports LangGraph"
        assert "copilotkit" not in text.lower(), f"{path} imports CopilotKit"


def test_langgraph_imports_only_in_executor_adapter():
    hits: list[Path] = []
    agents = ROOT / "redibis/agents"
    for path in agents.rglob("*.py"):
        if _LANGGRAPH_IMPORT.search(path.read_text(encoding="utf-8")):
            hits.append(path)
    assert set(hits) == _LANGGRAPH_ALLOWED


def test_executor_import_is_lazy():
    """Importing agents open-core surface must not load langgraph."""
    import sys

    before = {m for m in sys.modules if m.startswith("langgraph")}
    from redibis.agents import PipelineSpec, validate_spec, compile_prompt_plan

    assert PipelineSpec is not None
    assert callable(validate_spec)
    assert callable(compile_prompt_plan)
    after = {m for m in sys.modules if m.startswith("langgraph")}
    assert before == after


_ENTERPRISE_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(?:redibis_codegen_service|redibis_enterprise|enterprise)\b",
    re.MULTILINE,
)


def test_oss_package_never_imports_enterprise_distributions():
    """Tier A must not depend on vendor Tier B/C import names."""
    hits: list[str] = []
    for path in (ROOT / "redibis").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _ENTERPRISE_IMPORT.search(text):
            hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"OSS imports enterprise packages: {hits}"


def test_setuptools_excludes_enterprise_tree():
    """pyproject + MANIFEST.in keep enterprise/ out of the OSS distribution."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "enterprise*" in pyproject
    assert "redibis_codegen_service*" in pyproject
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-exclude enterprise" in manifest
