"""Pluggable quality result-sink strategy — where a monitor run's results go.

Built-in sinks: ``console`` (log-only, zero dependencies) and
``openmetadata`` (test suites/cases/results). Register a new one with
``@register_quality_sink("name")`` or the ``redibis.quality_sinks`` entry
point group; see ``redibis.quality.sinks.registry``.
"""

from redibis.quality.sinks.base import QualityResultSink, QualitySinkOptions
from redibis.quality.sinks.registry import (
    get_quality_sink_class,
    quality_sink_registry,
    register_quality_sink,
    resolve_sink_names,
)

__all__ = [
    "QualityResultSink",
    "QualitySinkOptions",
    "get_quality_sink_class",
    "quality_sink_registry",
    "register_quality_sink",
    "resolve_sink_names",
]
