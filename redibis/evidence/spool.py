"""Local restricted-evidence spool paths (leaf: no scan/store/cli imports)."""

from __future__ import annotations

from pathlib import Path

DEFAULT_OUTPUT_DIR = Path("./reports")
DEFAULT_SPOOL_DIR = DEFAULT_OUTPUT_DIR / "_restricted_evidence"
AUDIT_REL = "_audit/restricted_reads.jsonl"


def spool_root(spool_dir: Path | str | None) -> Path:
    if spool_dir is None or str(spool_dir).strip() == "":
        return DEFAULT_SPOOL_DIR
    return Path(spool_dir)


def run_spool_dir(spool_dir: Path | str | None, run_id: str) -> Path:
    rid = (run_id or "unbound").strip().replace("/", "_").replace("\\", "_") or "unbound"
    return spool_root(spool_dir) / rid


def llm_spool_dir(spool_dir: Path | str | None, run_id: str) -> Path:
    return run_spool_dir(spool_dir, run_id) / "llm_calls"


def audit_log_path(spool_dir: Path | str | None) -> Path:
    return spool_root(spool_dir) / AUDIT_REL
