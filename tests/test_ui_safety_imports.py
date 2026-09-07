"""Verify frozen webapp import surface (UI_SAFETY_CHECKLIST §1)."""

from __future__ import annotations

import subprocess
from pathlib import Path


REQUIRED = [
    "session_manager",
    "GlobalConfig",
    "sse_log_generator",
    "execute_unified_scan",
    "execute_discovery_run",
    "merge_session_sub_contracts",
    "build_approved_partials",
    "merge_approved",
    "pii_row_to_fragment",
    "quality_row_to_fragment",
    "_load_dataframe",
]


def test_webapp_session_service_imports_resolve():
    root = Path(__file__).resolve().parents[1]
    backend = root / "redibis" / "webapp" / "backend.py"
    text = backend.read_text(encoding="utf-8")
    assert "session_service" in text

    for name in REQUIRED:
        mod = __import__("redibis.services.session_service", fromlist=[name])
        assert hasattr(mod, name), f"missing re-export: {name}"


def test_grep_backend_imports_match_checklist():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        ["grep", "-n", "session_service", str(root / "redibis" / "webapp" / "backend.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = proc.stdout
    for token in (
        "session_manager",
        "execute_unified_scan",
        "execute_discovery_run",
        "merge_session_sub_contracts",
        "build_approved_partials",
        "merge_approved",
        "pii_row_to_fragment",
        "quality_row_to_fragment",
        "_load_dataframe",
    ):
        assert token in lines
