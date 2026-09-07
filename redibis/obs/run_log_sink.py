"""Persist per-run log buffers alongside the in-memory SSE capture."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from typing import Iterator, Optional

from redibis.obs.context import bind_context
from redibis.services.run_logging import capture_run_log

log = logging.getLogger(__name__)


def _mirror_meta_log(path: Path, content: str, table: str, run_id: str) -> None:
    """Best-effort local FS mirror under ``_meta/logs/`` (not routed to S3/MinIO).

    On object-storage deployments the durable per-scan log is
    ``<run_dir>/<table>.<run_id>.log`` on the run volume; this mirror is a
    convenience for local/_meta overlay layouts only.
    """
    try:
        meta_dir = path.parent / "_meta" / "logs" / table.replace(".", "/")
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{run_id}.log").write_text(content, encoding="utf-8")
    except Exception:
        pass


@contextmanager
def persist_run_log(
    run_dir: Path,
    table: str,
    run_id: str,
    *,
    jsonl: bool = True,
) -> Iterator[StringIO]:
    """Wrap ``capture_run_log`` and flush the buffer to disk on exit."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = run_dir / f"{table}.{run_id}.log"

    with bind_context(run_id=run_id, table=table), capture_run_log() as buf:
        try:
            yield buf
        finally:
            try:
                buf.seek(0)
                content = buf.read()
                if content and not content.endswith("\n"):
                    content += "\n"
                dest.write_text(content, encoding="utf-8")
                _mirror_meta_log(dest, content, table, run_id)
            except Exception as exc:
                log.debug("persist_run_log failed (non-fatal): %s", exc)
