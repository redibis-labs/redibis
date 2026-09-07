"""Cooperative cancellation tokens — checked between pipeline steps."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CancellationToken:
    """Thread-safe cancel flag with clean resumable boundaries."""

    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    reason: str = ""
    cancelled_at: str = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "") -> None:
        self.reason = reason or "cancelled by user"
        self.cancelled_at = _utc_iso()
        self._event.set()

    def check(self) -> None:
        """Raise if cancellation was requested."""
        if self.cancelled:
            raise RunCancelled(self.reason or "run cancelled")

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout=timeout)


class RunCancelled(Exception):
    """Raised when a batch run is cooperatively cancelled."""
