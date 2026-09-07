"""
redibis.profiling
=================
Pluggable profiling strategies (GE default; OpenMetadata via ``open_metadata``;
DuckDB SUMMARIZE via ``duckdb``).
"""

from __future__ import annotations

from redibis.config import ConfigError, ProfilingConfig
from redibis.profiling.base import ProfileResult, Profiler
from redibis.profiling import triage
from redibis.profiling.registry import get_profiler_class, profiler_registry, register_profiler

# Import concretes so @register_profiler runs before first get_profiler() call.
from redibis.profiling import great_expectations as _ge_profiler  # noqa: F401
from redibis.profiling import openmetadata as _om_profiler  # noqa: F401
from redibis.profiling import duckdb_profiler as _duckdb_profiler  # noqa: F401
from redibis.profiling.great_expectations import GreatExpectationsProfiler
from redibis.profiling.duckdb_profiler import DuckDbProfiler
from redibis.profiling.openmetadata import OpenMetadataProfiler

PROFILER_REGISTRY = profiler_registry()


def get_profiler(config: ProfilingConfig) -> Profiler:
    engine = (config.engine or "great_expectations").lower()
    cls = get_profiler_class(engine)
    if cls is None:
        raise ConfigError(
            f"unknown profiler engine {config.engine!r}; "
            f"choices: {sorted(PROFILER_REGISTRY)}"
        )
    return cls(config)


__all__ = [
    "ProfileResult",
    "Profiler",
    "GreatExpectationsProfiler",
    "DuckDbProfiler",
    "OpenMetadataProfiler",
    "PROFILER_REGISTRY",
    "get_profiler",
    "register_profiler",
    "triage",
]
