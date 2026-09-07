"""
redibis.memory.async_writer — background execution for memory store writes.
"""

from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

_executor: Optional[ThreadPoolExecutor] = None
_lock = threading.Lock()
_stats_lock = threading.Lock()
_stats = {
    "queue_pending": 0,
    "tasks_submitted": 0,
    "tasks_completed": 0,
    "tasks_failed": 0,
    "writes_succeeded": 0,
    "writes_failed": 0,
}


@dataclass(frozen=True)
class MemoryWriteStats:
    queue_pending: int
    tasks_submitted: int
    tasks_completed: int
    tasks_failed: int
    writes_succeeded: int
    writes_failed: int

    def to_dict(self) -> dict:
        return {
            "queue_pending": self.queue_pending,
            "tasks_submitted": self.tasks_submitted,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "writes_succeeded": self.writes_succeeded,
            "writes_failed": self.writes_failed,
        }


def memory_write_stats() -> MemoryWriteStats:
    with _stats_lock:
        return MemoryWriteStats(**dict(_stats))


def record_memory_write_success() -> None:
    with _stats_lock:
        _stats["writes_succeeded"] += 1


def record_memory_write_failure(*, reason: str = "") -> None:
    with _stats_lock:
        _stats["writes_failed"] += 1
    if reason:
        log.warning("memory write failed: %s", reason)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="redibis-memory",
            )
            atexit.register(shutdown_memory_writer)
        return _executor


def submit_memory_task(fn: Callable[[], None]) -> None:
    """Queue a memory write task; failures are logged, never raised to callers."""
    try:
        with _stats_lock:
            _stats["tasks_submitted"] += 1
            _stats["queue_pending"] += 1
        _get_executor().submit(_run_safe, fn)
    except Exception as exc:
        with _stats_lock:
            _stats["tasks_failed"] += 1
        log.warning("memory async submit failed: %s", exc)


def _run_safe(fn: Callable[[], None]) -> None:
    try:
        fn()
        with _stats_lock:
            _stats["tasks_completed"] += 1
    except Exception as exc:
        with _stats_lock:
            _stats["tasks_failed"] += 1
        log.warning("memory async task failed: %s", exc)
    finally:
        with _stats_lock:
            _stats["queue_pending"] = max(0, _stats["queue_pending"] - 1)


def flush_memory_writes(*, timeout: float = 30.0) -> None:
    """Block until queued memory writes finish (tests / graceful shutdown)."""
    ex = _executor
    if ex is None:
        return
    future = ex.submit(lambda: None)
    future.result(timeout=timeout)


def shutdown_memory_writer(*, wait: bool = True) -> None:
    global _executor
    with _lock:
        if _executor is None:
            return
        ex = _executor
        _executor = None
    ex.shutdown(wait=wait, cancel_futures=False)
