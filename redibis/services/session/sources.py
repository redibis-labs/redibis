"""Named session roots for loading flushed scan/agent sessions from disk."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

RootResolver = Callable[[], Path]


def _resolve_agent_root() -> Path:
    env = os.getenv("AGENT_OUTPUT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    cfg_path = os.environ.get("REDIBIS_CONFIG")
    if cfg_path:
        try:
            from redibis.config import RedibisConfig

            return Path(RedibisConfig.from_yaml(cfg_path).report.output_dir).resolve()
        except Exception:
            pass
    return Path("./agent_runs").expanduser().resolve()


@dataclass(frozen=True)
class SessionSource:
    """A named root directory whose children follow the standard session layout."""

    name: str
    label: str
    root_env: str
    default_root: str
    _resolver: Optional[RootResolver] = field(default=None, repr=False, compare=False)

    def root(self) -> Path:
        if self._resolver is not None:
            return self._resolver()
        return Path(os.getenv(self.root_env, self.default_root)).expanduser().resolve()

    def normalize(self, session_dir: Path) -> None:
        """Hook for sources whose on-disk layout differs from the scan layout (no-op by default)."""


_SOURCES: dict[str, SessionSource] = {
    "scan": SessionSource("scan", "Scan runs", "SCAN_OUTPUT_DIR", "./scan_output"),
    "agent": SessionSource(
        "agent",
        "Agent runs",
        "AGENT_OUTPUT_DIR",
        "./agent_runs",
        _resolver=_resolve_agent_root,
    ),
}


def get_source(name: str) -> SessionSource:
    try:
        return _SOURCES[name]
    except KeyError as exc:
        raise KeyError(f"Unknown session source {name!r}") from exc


def list_sources() -> list[SessionSource]:
    return list(_SOURCES.values())


def list_source_roots() -> list[Path]:
    """All configured roots (for artifact path containment checks)."""
    roots: list[Path] = []
    seen: set[str] = set()
    for src in _SOURCES.values():
        root = src.root()
        key = str(root)
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def register_source(src: SessionSource) -> None:
    _SOURCES[src.name] = src
