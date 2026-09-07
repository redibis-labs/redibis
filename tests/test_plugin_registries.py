"""Tests for profiler/validator plugin registries."""

from __future__ import annotations

from redibis.config import ProfilingConfig
from redibis.profiling import PROFILER_REGISTRY, get_profiler
from redibis.profiling.openmetadata import OpenMetadataProfiler
from redibis.profiling.registry import profiler_registry, register_profiler
from redibis.profiling.base import Profiler, ProfileResult
from redibis.quality.registry import validator_registry


def test_builtin_profilers_registered():
    assert "great_expectations" in PROFILER_REGISTRY
    assert "open_metadata" in PROFILER_REGISTRY
    assert "duckdb" in PROFILER_REGISTRY
    p = get_profiler(ProfilingConfig(engine="open_metadata"))
    assert isinstance(p, OpenMetadataProfiler)


def test_register_profiler_decorator():
    @register_profiler("test_profiler_plugin")
    class _Stub(Profiler):
        name = "test_profiler_plugin"

        def profile(self, df, *, dataset_name: str) -> ProfileResult:
            raise NotImplementedError

    assert "test_profiler_plugin" in profiler_registry()


def test_builtin_validators_registered():
    reg = validator_registry()
    assert "great_expectations" in reg
