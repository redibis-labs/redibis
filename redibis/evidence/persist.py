"""Local dual-copy persistence for LLM call evidence (no storage backend)."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from redibis.evidence.models import LLMCallEvidence
from redibis.evidence.redact import restricted_llm_call, shareable_llm_call
from redibis.evidence.spool import llm_spool_dir, spool_root

RAW_SUFFIX = ".raw.json"
SHAREABLE_SUFFIX = ".json"

DIR_MODE = 0o700
FILE_MODE = 0o600


def llm_calls_dir(run_dir: Path) -> Path:
    return Path(run_dir) / "llm_calls"


def call_filename(seq: int, call_id: str, *, raw: bool) -> str:
    stem = f"{int(seq):04d}-{call_id}"
    return f"{stem}{RAW_SUFFIX if raw else SHAREABLE_SUFFIX}"


def ensure_restricted_dir(path: Path) -> Path:
    """Create *path* and set mode ``0700`` on it."""
    dest = Path(path)
    dest.mkdir(parents=True, exist_ok=True)
    chmod_restricted_dir(dest)
    return dest


def chmod_restricted_dir(path: Path) -> None:
    try:
        os.chmod(Path(path), DIR_MODE)
    except OSError:
        pass


def chmod_restricted_file(path: Path) -> None:
    try:
        os.chmod(Path(path), FILE_MODE)
    except OSError:
        pass


def atomic_write_json(path: Path, payload: Any, *, restricted: bool = False) -> None:
    """Write JSON via a sibling temp file then ``os.replace``."""
    path = Path(path)
    if restricted:
        ensure_restricted_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    if restricted:
        chmod_restricted_file(path)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Append-safe exclusive lock around concurrent recorder writes."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError, ValueError):
            pass
        fh.close()


def write_llm_call_files(
    run_dir: Optional[Path],
    record: dict[str, Any] | LLMCallEvidence,
    *,
    seq: int,
    spool_dir: Optional[Path] = None,
    run_id: str = "",
) -> dict[str, str]:
    """Write restricted ``.raw.json`` to the governed spool and shareable ``.json``.

    Exact records never land in ordinary run folders. Shareable copies go to
    *run_dir* when provided; otherwise they sit next to the spool copy.
    """
    payload = record.to_dict() if isinstance(record, LLMCallEvidence) else dict(record)
    call_id = str(payload.get("call_id") or f"seq{seq}")
    raw_name = call_filename(seq, call_id, raw=True)
    share_name = call_filename(seq, call_id, raw=False)
    raw_payload = restricted_llm_call(payload)
    share_payload = shareable_llm_call(payload)
    out: dict[str, str] = {
        "call_id": call_id,
        "seq": str(seq),
        "rel_raw": f"llm_calls/{raw_name}",
        "rel_shareable": f"llm_calls/{share_name}",
    }

    share_written = False
    if run_dir is not None:
        directory = llm_calls_dir(Path(run_dir))
        directory.mkdir(parents=True, exist_ok=True)
        share_path = directory / share_name
        atomic_write_json(share_path, share_payload)
        share_written = True
        out["shareable"] = str(share_path)

    rid = run_id or str(payload.get("run_id") or "") or "unbound"
    spool = llm_spool_dir(spool_dir if spool_dir is not None else spool_root(None), rid)
    ensure_restricted_dir(spool)
    chmod_restricted_dir(spool.parent)
    spool_raw = spool / raw_name
    atomic_write_json(spool_raw, raw_payload, restricted=True)
    out["spool_raw"] = str(spool_raw)
    out["raw"] = str(spool_raw)
    if not share_written:
        spool_share = spool / share_name
        atomic_write_json(spool_share, share_payload, restricted=True)
        out["shareable"] = str(spool_share)

    return out


def is_raw_artifact(name: str) -> bool:
    lower = str(name or "").replace("\\", "/").lower()
    base = Path(lower).name
    if lower.endswith(RAW_SUFFIX) or lower.endswith(".raw.json"):
        return True
    if base == "evidence_bundle.json":
        return True
    return False
