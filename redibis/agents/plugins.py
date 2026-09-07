"""Agent tool plugin registry — open/commercial seam (entry-point ``redibis.agent_tools``)."""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from redibis.agents.node_registry import NodeSpec


class AgentToolPlugin(ABC):
    """Commercial or community extensions register new board nodes + runners."""

    @property
    @abstractmethod
    def node_spec(self) -> NodeSpec:
        """Palette metadata (kind must be unique)."""

    @abstractmethod
    def run(self, node_params: dict[str, Any], *, table: str, context: Any) -> dict[str, Any]:
        """Execute the tool; return step output dict."""


RunnerFn = Callable[..., dict[str, Any]]
_ENTRY_POINT_GROUP = "redibis.agent_tools"
_PLUGIN_RUNNERS: dict[str, RunnerFn] = {}


def register_plugin_runner(kind: str) -> Callable[[RunnerFn], RunnerFn]:
    """Decorator for in-tree plugin runners bound to a node kind."""

    def _decorator(fn: RunnerFn) -> RunnerFn:
        _PLUGIN_RUNNERS[kind] = fn
        return fn

    return _decorator


def _load_entry_point_plugins() -> tuple[dict[str, NodeSpec], dict[str, RunnerFn]]:
    specs: dict[str, NodeSpec] = {}
    runners: dict[str, RunnerFn] = {}

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
        obj = ep.load()
        if isinstance(obj, type) and issubclass(obj, AgentToolPlugin):
            plugin = obj()
            spec = plugin.node_spec
            specs[spec.kind] = spec
            runners[spec.kind] = lambda np, table, ctx, p=plugin: p.run(np, table=table, context=ctx)
        elif isinstance(obj, NodeSpec):
            specs[obj.kind] = obj
        elif callable(obj):
            kind = ep.name
            specs[kind] = NodeSpec(kind=kind, label=ep.name, tool=f"plugin:{kind}")
            runners[kind] = obj
    return specs, runners


def plugin_registry() -> dict[str, NodeSpec]:
    """Entry-point plugin node specs (does not include builtins)."""
    specs, _ = _load_entry_point_plugins()
    return specs


def plugin_runners() -> dict[str, RunnerFn]:
    """Merged entry-point + decorator-registered runners."""
    _, ep_runners = _load_entry_point_plugins()
    merged = dict(ep_runners)
    merged.update(_PLUGIN_RUNNERS)
    return merged


def get_plugin_runner(kind: str) -> Optional[RunnerFn]:
    return plugin_runners().get(kind)


def register_runner(kind: str, fn: RunnerFn) -> None:
    """Register a tool runner at runtime (dynamic tools, tests)."""
    _PLUGIN_RUNNERS[kind] = fn
