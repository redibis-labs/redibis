"""Tests for context-local run log capture."""

from __future__ import annotations

import logging
import threading

from redibis.services.run_logging import capture_run_log


def test_capture_run_log_collects_records():
    log = logging.getLogger("redibis.test.capture")
    with capture_run_log() as buf:
        log.info("hello-scan")
    assert "hello-scan" in buf.getvalue()


def test_concurrent_capture_run_logs_are_isolated():
    log = logging.getLogger("redibis.test.concurrent")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def worker(name: str) -> None:
        with capture_run_log() as buf:
            barrier.wait()
            log.info(f"msg-{name}")
            results[name] = buf.getvalue()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert "msg-a" in results["a"]
    assert "msg-b" in results["b"]
    assert "msg-b" not in results["a"]
    assert "msg-a" not in results["b"]
