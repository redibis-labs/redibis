"""Context-local log capture for concurrent scan runs."""

from __future__ import annotations

import contextvars
import logging
import threading
from contextlib import contextmanager
from io import StringIO
from typing import Iterator, Optional

_run_log_buffer: contextvars.ContextVar[Optional[StringIO]] = contextvars.ContextVar(
    "redibis_run_log_buffer",
    default=None,
)

_HANDLER: Optional[logging.Handler] = None
_LOCK = threading.Lock()
_CAPTURE_REFCOUNT = 0
_SAVED_ROOT_LEVEL = logging.WARNING


class _ContextRunLogHandler(logging.Handler):
    """Writes log records to the ``StringIO`` bound in the current context."""

    def emit(self, record: logging.LogRecord) -> None:
        buf = _run_log_buffer.get()
        if buf is None:
            return
        try:
            buf.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


def _ensure_handler() -> _ContextRunLogHandler:
    global _HANDLER
    if _HANDLER is None:
        handler = _ContextRunLogHandler()
        handler.setLevel(logging.NOTSET)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        setattr(handler, "redibis_handler_kind", "redibis.obs.capture")
        _HANDLER = handler
    root = logging.getLogger()
    if _HANDLER not in root.handlers:
        root.addHandler(_HANDLER)
    return _HANDLER


def _enter_capture() -> None:
    global _CAPTURE_REFCOUNT, _SAVED_ROOT_LEVEL
    with _LOCK:
        if _CAPTURE_REFCOUNT == 0:
            root = logging.getLogger()
            _SAVED_ROOT_LEVEL = root.level
            root.setLevel(logging.DEBUG)
        _CAPTURE_REFCOUNT += 1


def _exit_capture() -> None:
    global _CAPTURE_REFCOUNT
    with _LOCK:
        _CAPTURE_REFCOUNT -= 1
        if _CAPTURE_REFCOUNT == 0:
            logging.getLogger().setLevel(_SAVED_ROOT_LEVEL)


@contextmanager
def capture_run_log() -> Iterator[StringIO]:
    """
    Capture root-logger output for the current task/thread.

    A refcount temporarily lowers the root logger to ``DEBUG`` so third-party
    libraries (GE, Presidio) are captured; concurrent scans share the refcount
    safely while each writes to its own context-local buffer.
    """
    buf = StringIO()
    token = _run_log_buffer.set(buf)
    _ensure_handler()
    _enter_capture()
    try:
        yield buf
    finally:
        _run_log_buffer.reset(token)
        _exit_capture()
