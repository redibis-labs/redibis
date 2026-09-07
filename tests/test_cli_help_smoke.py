"""Smoke-test ``main()`` --help for every CLI subcommand path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from redibis.cli.main import main

ROOT = Path(__file__).resolve().parent.parent

# (argv tail after ``redibis``) — each must exit 0 with --help
_HELP_PATHS: list[list[str]] = [
    ["--help"],
    ["import-business", "--help"],
    ["merge", "--help"],
    ["show", "--help"],
    ["history", "--help"],
    ["list", "--help"],
    ["scan", "--help"],
    ["scan", "evidence", "--help"],
    ["scan", "evidence", "store", "--help"],
    ["scan", "evidence", "packs", "--help"],
    ["scan", "evidence", "export", "--help"],
    ["scan", "evidence", "coverage", "--help"],
    ["scan", "evidence", "llm", "--help"],
    ["scan", "decide", "--help"],
    ["context", "build", "--help"],
    ["pack", "get", "--help"],
    ["pack", "list", "--help"],
    ["pack", "show", "--help"],
    ["pack", "history", "--help"],
    ["pack", "diff", "--help"],
    ["pack", "publish", "--help"],
    ["profile", "--help"],
    ["quality", "--help"],
    ["config", "dump-default", "--help"],
    ["runs", "list", "--help"],
    ["runs", "merge", "--help"],
    ["runs", "discard", "--help"],
    ["approved", "list", "--help"],
    ["approved", "show", "--help"],
    ["approved", "add-pii", "--help"],
    ["approved", "add-quality", "--help"],
    ["approved", "remove", "--help"],
    ["approved", "preview", "--help"],
    ["approved", "merge", "--help"],
    ["approved", "clear", "--help"],
    ["rules", "list", "--help"],
    ["rules", "export", "--help"],
    ["quality-monitor", "--help"],
    ["quality-monitor", "run", "--help"],
    ["quality-monitor", "batch", "--help"],
    ["quality-monitor", "export", "--help"],
    ["quality-monitor", "airflow", "generate", "--help"],
    ["monitor", "--help"],
    ["monitor", "export", "--help"],
    ["retention", "show", "--help"],
    ["retention", "set", "--help"],
    ["retention", "clear", "--help"],
    ["contract", "purge", "--help"],
    ["contract", "metadata", "--help"],
    ["contract", "export-package", "--help"],
    ["contract", "pii-view", "--help"],
    ["contract", "quality-view", "--help"],
    ["contract", "definitions-view", "--help"],
    ["contract", "strip-pii", "--help"],
    ["contract", "add-pii", "--help"],
    ["contract", "quality-suppress", "--help"],
    ["contract", "quality-suppress-all", "--help"],
    ["contract", "quality-restore", "--help"],
    ["contract", "definitions-patch", "--help"],
    ["enrich", "--help"],
    ["data", "preview", "--help"],
    ["mask", "plan", "--help"],
    ["mask", "apply", "--help"],
    ["mask", "auto", "--help"],
    ["mask", "regex", "list", "--help"],
    ["mask", "regex", "test", "--help"],
    ["models", "list", "--help"],
    ["models", "upload", "--help"],
    ["models", "activate", "--help"],
    ["models", "delete", "--help"],
    ["memory", "init-db", "--help"],
    ["memory", "status", "--help"],
    ["memory", "search", "--help"],
    ["behavior", "catalog", "--help"],
    ["behavior", "status", "--help"],
    ["behavior", "validate", "--help"],
    ["behavior", "list", "--help"],
    ["behavior", "get", "--help"],
    ["behavior", "create", "--help"],
    ["behavior", "simulate", "--help"],
    ["behavior", "approve", "--help"],
    ["behavior", "activate", "--help"],
    ["behavior", "deactivate", "--help"],
    ["behavior", "rollback", "--help"],
    ["behavior", "promote", "--help"],
    ["behavior", "history", "--help"],
    ["behavior", "audit", "--help"],
    ["behavior", "metrics", "--help"],
    ["behavior", "plugins", "--help"],
    ["behavior", "outcomes", "list", "--help"],
    ["behavior", "outcomes", "record", "--help"],
    ["behavior", "signals", "--help"],
    ["behavior", "suggest", "--help"],
    ["behavior", "draft-from", "--help"],
]


@pytest.mark.parametrize("argv_tail", _HELP_PATHS, ids=lambda p: " ".join(p))
def test_cli_help_exits_zero(argv_tail: list[str]):
    with pytest.raises(SystemExit) as exc:
        main(argv_tail)
    assert exc.value.code == 0


def test_cli_help_cold_process_avoids_heavy_imports():
    """Fresh interpreter: ``main(['--help'])`` must not load GE / scan pipeline."""
    code = """
import sys

def _loaded(prefix: str) -> bool:
    return any(n == prefix or n.startswith(prefix + ".") for n in sys.modules)

from redibis.cli.main import main

try:
    main(["--help"])
except SystemExit as exc:
    assert exc.code == 0, exc.code
else:
    raise AssertionError("expected SystemExit from --help")

forbidden = (
    "great_expectations",
    "scipy",
    "pyspark",
    "presidio_analyzer",
    "redibis.services.pipeline",
    "redibis.quality.gatekeeper",
    "redibis.obs.run_log_sink",
    "redibis.services.scan_service",
)
for name in forbidden:
    assert not _loaded(name), f"unexpectedly loaded {name}"
print("ok")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

