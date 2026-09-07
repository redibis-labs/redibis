"""Quality result-sink registry — built-in sinks + optional entry-point plugins.

Mirrors ``redibis.profiling.registry`` / ``redibis.quality.registry`` so
adding a destination for monitor results follows the same plugin shape as
adding a profiler or validator engine.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Callable

from redibis.config import ConfigError

if TYPE_CHECKING:
    from redibis.config import RedibisConfig
    from redibis.quality.sinks.base import QualityResultSink

_BUILTIN: dict[str, type] = {}
_ENTRY_POINT_GROUP = "redibis.quality_sinks"


def register_quality_sink(name: str) -> Callable[[type], type]:
    """Decorator: ``@register_quality_sink("openmetadata")``."""

    def _decorator(cls: type) -> type:
        _BUILTIN[name.lower()] = cls
        return cls

    return _decorator


def _load_entry_point_sinks() -> dict[str, type]:
    loaded: dict[str, type] = {}
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
        from redibis.quality.sinks.base import QualityResultSink

        if not isinstance(cls, type) or not issubclass(cls, QualityResultSink):
            raise ConfigError(
                f"quality sink entry point {ep.name!r} must resolve to a QualityResultSink subclass"
            )
        loaded[ep.name.lower()] = cls
    return loaded


def _ensure_builtins_loaded() -> None:
    if not _BUILTIN:
        # Import for side-effecting @register_quality_sink decorators.
        from redibis.quality.sinks import console_sink, openmetadata_sink  # noqa: F401


def quality_sink_registry() -> dict[str, type]:
    _ensure_builtins_loaded()
    merged = _load_entry_point_sinks()
    merged.update(_BUILTIN)
    return dict(merged)


def get_quality_sink_class(name: str) -> type:
    key = (name or "").lower()
    cls = quality_sink_registry().get(key)
    if cls is None:
        raise ConfigError(
            f"unknown quality result sink {name!r}; choices: {sorted(quality_sink_registry())}"
        )
    return cls


def resolve_sink_names(config: "RedibisConfig") -> list[str]:
    """Resolve the configured sink names.

    ``quality.publish.sinks`` is authoritative when set. Otherwise, fall
    back to the legacy ``catalog.push.quality`` toggle for backward
    compatibility (``True`` -> ``["openmetadata"]``, ``False`` -> ``[]``).
    """
    publish_cfg = getattr(config.quality, "publish", None)
    explicit = list(getattr(publish_cfg, "sinks", None) or [])
    if explicit:
        return explicit
    if config.catalog.push.quality:
        return ["openmetadata"]
    return []


__all__ = [
    "register_quality_sink",
    "quality_sink_registry",
    "get_quality_sink_class",
    "resolve_sink_names",
]
