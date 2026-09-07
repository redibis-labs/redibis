"""Observability — structured logging, decision channel, per-run log persistence."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from redibis.obs.context import bind_context
from redibis.obs.decision import DecisionRecord, decision, sanitize_inputs, sanitize_text, set_decision_log_enabled
from redibis.obs.logging_setup import setup_logging

if TYPE_CHECKING:
    from redibis.obs.run_log_sink import persist_run_log as persist_run_log

get_logger = logging.getLogger

__all__ = [
    "bind_context",
    "decision",
    "DecisionRecord",
    "get_logger",
    "persist_run_log",
    "sanitize_inputs",
    "sanitize_text",
    "set_decision_log_enabled",
    "setup_logging",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "persist_run_log": ("redibis.obs.run_log_sink", "persist_run_log"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        import importlib

        module_name, attr = _LAZY_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
