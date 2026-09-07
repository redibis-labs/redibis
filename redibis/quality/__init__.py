"""redibis.quality — GE-based sampling + profiling + quality contracts."""

from __future__ import annotations

from typing import Any

from redibis.quality.rule_set import QualityRuleSet  # noqa: F401

__all__ = [
    "QualityRuleSet",
    "TableSampler",
    "PandasTableSampler",
    "SamplingConfig",
]

_SAMPLING_EXPORTS = frozenset({"TableSampler", "PandasTableSampler", "SamplingConfig"})


def __getattr__(name: str) -> Any:
    if name in _SAMPLING_EXPORTS:
        from redibis.quality.sampling import (  # noqa: PLC0415
            PandasTableSampler,
            SamplingConfig,
            TableSampler,
        )

        mapping = {
            "TableSampler": TableSampler,
            "PandasTableSampler": PandasTableSampler,
            "SamplingConfig": SamplingConfig,
        }
        value = mapping[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
