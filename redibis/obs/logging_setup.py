"""Idempotent root logger setup for CLI, webapp, and library use."""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from redibis.obs.context import ContextInjectFilter
from redibis.obs.formatters import JsonFormatter

_CONFIGURED = False
_OBS_CONSOLE_TAG = "redibis.obs.console"

# Correlation fields injected by ContextInjectFilter — visible in rich/plain too.
_PLAIN_FMT = (
    "%(asctime)s %(levelname)s [%(run_id)s/%(table)s] %(name)s: %(message)s"
)
_RICH_FMT = "[%(run_id)s/%(table)s] %(message)s"


def _resolve_level(level: Optional[str]) -> int:
    raw = (level or os.environ.get("REDIBIS_LOG_LEVEL") or "INFO").upper()
    return getattr(logging, raw, logging.INFO)


def _resolve_format(fmt: Optional[str]) -> str:
    if fmt:
        return fmt.lower()
    env = (os.environ.get("REDIBIS_LOG_FORMAT") or "").lower()
    if env in ("rich", "json", "plain"):
        return env
    return "rich" if sys.stderr.isatty() else "json"


def _is_obs_console_handler(handler: logging.Handler) -> bool:
    return getattr(handler, "redibis_handler_kind", None) == _OBS_CONSOLE_TAG


def _build_handler(fmt: str, *, level: int) -> logging.Handler:
    if fmt == "rich":
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            rich_tracebacks=True,
            show_path=False,
            markup=False,
        )
        handler.setFormatter(logging.Formatter(_RICH_FMT, datefmt="[%X]"))
    elif fmt == "json":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_PLAIN_FMT, datefmt="%H:%M:%S"))
    handler.addFilter(ContextInjectFilter())
    handler.setLevel(level)
    setattr(handler, "redibis_handler_kind", _OBS_CONSOLE_TAG)
    return handler


def _apply_module_levels(module_levels: Optional[dict[str, str]]) -> None:
    env = os.environ.get("REDIBIS_LOG_MODULES", "")
    levels = dict(module_levels or {})
    if env:
        for part in env.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, lvl = part.split("=", 1)
            levels[name.strip()] = lvl.strip()
    for name, lvl in levels.items():
        numeric = getattr(logging, str(lvl).upper(), None)
        if numeric is not None:
            logging.getLogger(name).setLevel(numeric)


def setup_logging(
    level: Optional[str] = None,
    fmt: Optional[str] = None,
    *,
    force: bool = False,
    module_levels: Optional[dict[str, str]] = None,
    decision_log: bool = True,
) -> None:
    """Configure the root logger once (safe to call twice with ``force=True``)."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    from redibis.obs.decision import set_decision_log_enabled

    resolved_level = _resolve_level(level)
    resolved_fmt = _resolve_format(fmt)
    root = logging.getLogger()

    # Replace only our console handler — preserve capture_run_log and third-party handlers.
    root.handlers[:] = [h for h in root.handlers if not _is_obs_console_handler(h)]

    handler = _build_handler(resolved_fmt, level=resolved_level)
    root.addHandler(handler)
    root.setLevel(resolved_level)
    _apply_module_levels(module_levels)
    set_decision_log_enabled(decision_log)
    _CONFIGURED = True
