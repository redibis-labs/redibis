"""OpenTelemetry + Responsible-AI hooks for the agent layer."""

from redibis.config import RAIConfig
from redibis.telemetry.models import ModelCallRecord, SpanRecord
from redibis.telemetry.otel import TelemetryCollector, run_context
from redibis.telemetry.init import init_otel, get_tracer, get_meter
from redibis.telemetry.rai import RAIDecision, RAIMiddleware

__all__ = [
    "ModelCallRecord",
    "RAIConfig",
    "RAIDecision",
    "RAIMiddleware",
    "SpanRecord",
    "TelemetryCollector",
    "run_context",
    "init_otel",
    "get_tracer",
    "get_meter",
]
