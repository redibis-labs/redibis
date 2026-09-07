"""Small stdlib timeouts for outbound dependency calls."""

from __future__ import annotations

import concurrent.futures
from typing import Callable, TypeVar

T = TypeVar("T")


class DependencyTimeoutError(TimeoutError):
    """Raised when an outbound dependency call exceeds its deadline."""


def call_with_timeout(fn: Callable[..., T], timeout: float, *args, **kwargs) -> T:
    """Run *fn* in a worker thread; raise ``DependencyTimeoutError`` on expiry."""
    if timeout <= 0:
        return fn(*args, **kwargs)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise DependencyTimeoutError(
            f"dependency call timed out after {timeout}s"
        ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
