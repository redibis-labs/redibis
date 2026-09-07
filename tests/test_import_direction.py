"""Import-direction guards from the unified config remediation plan."""

from __future__ import annotations

import ast
from pathlib import Path


def _top_level_imports(module_path: Path, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for prefix in forbidden_prefixes:
                if node.module == prefix or node.module.startswith(prefix + "."):
                    hits.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for prefix in forbidden_prefixes:
                    if alias.name == prefix or alias.name.startswith(prefix + "."):
                        hits.append(alias.name)
    return hits


def test_config_has_no_service_or_cli_imports():
    root = Path(__file__).resolve().parents[1]
    config_py = root / "redibis" / "config.py"
    forbidden = ("redibis.services", "redibis.cli", "redibis.webapp")
    hits = _top_level_imports(config_py, forbidden)
    assert hits == [], f"redibis.config must not import {forbidden}, found: {hits}"


def test_models_has_no_pii_or_masking_imports():
    root = Path(__file__).resolve().parents[1]
    models_py = root / "redibis" / "models.py"
    forbidden = ("redibis.pii", "redibis.masking", "redibis.contracts")
    hits = _top_level_imports(models_py, forbidden)
    assert hits == [], f"redibis.models must not import {forbidden}, found: {hits}"
