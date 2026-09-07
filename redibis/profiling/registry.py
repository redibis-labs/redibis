"""
Profiler engine registry — built-in engines + optional entry-point plugins.
"""

from __future__ import annotations

import sys
from typing import Callable, Type

from redibis.config import ConfigError
from redibis.profiling.base import Profiler

_BUILTIN: dict[str, type[Profiler]] = {}
_ENTRY_POINT_GROUP = "redibis.profilers"


def register_profiler(name: str) -> Callable[[type[Profiler]], type[Profiler]]:
    """Decorator: ``@register_profiler("my_engine")``."""

    def _decorator(cls: type[Profiler]) -> type[Profiler]:
        key = name.lower()
        _BUILTIN[key] = cls
        return cls

    return _decorator


def _load_entry_point_profilers() -> dict[str, type[Profiler]]:
    loaded: dict[str, type[Profiler]] = {}
    if sys.version_info >= (3, 10):
        from importlib.metadata import entry_points

        eps = entry_points()
        group = (
            eps.select(group=_ENTRY_POINT_GROUP)
            if hasattr(eps, "select")
            else eps.get(_ENTRY_POINT_GROUP, [])
        )
    else:
        from importlib.metadata import entry_points as ep_legacy

        group = ep_legacy().get(_ENTRY_POINT_GROUP, [])

    for ep in group:
        cls = ep.load()
        if not isinstance(cls, type) or not issubclass(cls, Profiler):
            raise ConfigError(
                f"profiler entry point {ep.name!r} must resolve to a Profiler subclass"
            )
        loaded[ep.name.lower()] = cls
    return loaded


def profiler_registry() -> dict[str, type[Profiler]]:
    """Merged built-in + entry-point profilers (built-ins win on name clash)."""
    merged = _load_entry_point_profilers()
    merged.update(_BUILTIN)
    return dict(merged)


def get_profiler_class(engine: str) -> type[Profiler]:
    key = (engine or "great_expectations").lower()
    cls = profiler_registry().get(key)
    if cls is None:
        raise ConfigError(
            f"unknown profiler engine {engine!r}; "
            f"choices: {sorted(profiler_registry())}"
        )
    return cls
