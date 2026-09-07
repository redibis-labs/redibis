"""Guard config.py import direction (POST_IMPLEMENTATION_REVIEW §2.6)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_FORBIDDEN_PREFIXES = (
    "redibis.services",
    "redibis.cli",
    "redibis.webapp",
)


def _imports_in_config() -> list[str]:
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "redibis" / "config.py").read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_config_imports_no_services_cli_or_webapp():
    violations = [
        mod for mod in _imports_in_config()
        if any(mod == p or mod.startswith(p + ".") for p in _FORBIDDEN_PREFIXES)
    ]
    assert violations == [], f"config.py must not import: {violations}"


def test_config_validate_rejects_unknown_profiler(tmp_path):
    from redibis.config import ConfigError, RedibisConfig

    cfg = RedibisConfig.default()
    cfg.profiling.engine = "not_a_real_engine"
    with pytest.raises(ConfigError, match="profiler"):
        cfg.validate()


def test_config_validate_rejects_unknown_equation_mode():
    from redibis.config import ConfigError, RedibisConfig

    cfg = RedibisConfig.default()
    cfg.pii.equation_mode = "yolo"
    with pytest.raises(ConfigError, match="equation"):
        cfg.validate()


def test_config_validate_rejects_unknown_automerge():
    from redibis.config import ConfigError, RedibisConfig

    cfg = RedibisConfig.default()
    cfg.contract.automerge = "everything"
    with pytest.raises(ConfigError, match="automerge"):
        cfg.validate()
