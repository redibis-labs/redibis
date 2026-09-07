"""Tests for optional OpenTelemetry facade (Phase 4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from redibis.config import ObservabilityConfig, OtelConfig, RedibisConfig
from redibis.telemetry.init import init_otel
from redibis.telemetry.otel import TelemetryCollector, run_context


def test_init_otel_disabled_returns_none():
    cfg = RedibisConfig.default()
    tracer, meter = init_otel(cfg)
    assert tracer is None
    assert meter is None


def test_init_otel_import_error_graceful():
    cfg = RedibisConfig.default()
    cfg.observability.otel = OtelConfig(enabled=True)
    with patch.dict("sys.modules", {"opentelemetry": None}):
        tracer, meter = init_otel(cfg)
    assert tracer is None
    assert meter is None


def test_telemetry_collector_without_otel_unchanged():
    with run_context("run-1") as tel:
        with tel.span("step.a", key="v") as rec:
            rec.finish(extra="x")
    exported = tel.export()
    assert len(exported) == 1
    assert exported[0]["name"] == "step.a"
    assert exported[0]["span_id"]


def test_telemetry_collector_with_stub_tracer():
    stub_span = MagicMock()
    stub_span.get_span_context.return_value = MagicMock(span_id=0xABCDEF1234567890)
    stub_cm = MagicMock()
    stub_cm.__enter__ = MagicMock(return_value=stub_span)
    stub_cm.__exit__ = MagicMock(return_value=False)
    stub_tracer = MagicMock()
    stub_tracer.start_as_current_span.return_value = stub_cm

    collector = TelemetryCollector(run_id="r2", otel_tracer=stub_tracer)
    with collector.span("deep_scan.producer", producer="profile.ge") as rec:
        pass
    assert rec.span_id == format(0xABCDEF1234567890, "016x")
    assert len(collector.export()) == 1


def test_otlp_unreachable_degrades(monkeypatch):
    cfg = RedibisConfig.default()
    cfg.observability.otel = OtelConfig(
        enabled=True,
        exporter="otlp",
        otlp_endpoint="localhost:1",
    )

    class _BrokenExporter:
        def __init__(self, *a, **k):
            raise ConnectionError("unreachable")

    monkeypatch.setitem(
        __import__("sys").modules,
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        MagicMock(OTLPSpanExporter=_BrokenExporter),
    )
    # Should not raise — init_otel catches and falls back
    try:
        tracer, meter = init_otel(cfg)
    except Exception:
        pytest.fail("init_otel raised on unreachable OTLP endpoint")
