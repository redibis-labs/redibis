"""Bounded thread-pool executor for heavy scan / discovery jobs."""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger("redibis.webapp.jobs")

_MAX_WORKERS = int(os.getenv("REDIBIS_SCAN_WORKERS", "2"))
_MAX_QUEUE = int(os.getenv("REDIBIS_SCAN_QUEUE", "10"))

_JOBS = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="redibis-scan")
_inflight = 0
_inflight_lock = threading.Lock()

_RUNNING_LIKE = frozenset({
    "running",
    "profiling",
    "running_quality",
    "running_pii",
    "initialized",
})


class ScanQueueFullError(RuntimeError):
    """Raised when the scan job queue is saturated."""


def scan_queue_saturation() -> tuple[int, int]:
    """Return (in_flight, max_queue) for load shedding."""
    with _inflight_lock:
        return _inflight, _MAX_QUEUE


def submit_scan_job(
    fn: Callable[..., Any],
    *args: Any,
    session: Any = None,
    **kwargs: Any,
) -> Future:
    global _inflight
    with _inflight_lock:
        if _inflight >= _MAX_QUEUE + _MAX_WORKERS:
            raise ScanQueueFullError(
                f"scan queue saturated ({_inflight} in flight, max {_MAX_QUEUE + _MAX_WORKERS})"
            )
        _inflight += 1

    def _wrapped() -> None:
        global _inflight
        try:
            fn(*args, **kwargs)
            if session is not None and getattr(session, "status", "") in _RUNNING_LIKE:
                if hasattr(session, "set_status"):
                    session.set_status("complete")
        except Exception as exc:
            logger.exception("scan job failed")
            if session is not None:
                if getattr(session, "status", "") in _RUNNING_LIKE:
                    session.set_status("failed")
                if hasattr(session, "add_log"):
                    session.add_log(f"ERROR {exc}")
        finally:
            if session is not None:
                try:
                    session.persist_to_disk()
                except Exception:
                    logger.exception("persist after job failed")
            with _inflight_lock:
                _inflight -= 1

    return _JOBS.submit(_wrapped)


def shutdown_jobs(wait: bool = False) -> None:
    _JOBS.shutdown(wait=wait, cancel_futures=not wait)
