"""Invoke Node's built-in test runner for gateway_render.mjs when node is available."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "tests" / "js"


def test_gateway_render_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    files = sorted(str(p) for p in JS_DIR.glob("*.mjs"))
    proc = subprocess.run(
        [node, "--test", *files],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
