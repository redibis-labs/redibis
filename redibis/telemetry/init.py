"""Optional OpenTelemetry SDK bootstrap (``redibis[otel]`` extra)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

_tracer: Any = None
_meter: Any = None
_run_dir: Optional[Path] = None


def get_tracer() -> Any:
    return _tracer


def get_meter() -> Any:
    return _meter


def set_run_dir(run_dir: Optional[Path]) -> None:
    global _run_dir
    _run_dir = Path(run_dir) if run_dir else None


def init_otel(config: Any) -> Tuple[Any, Any]:
    """
    Initialise OTel tracer + meter when ``observability.otel.enabled``.

    Returns ``(tracer, meter)`` or ``(None, None)`` when disabled/unavailable.
    """
    global _tracer, _meter

    otel_cfg = getattr(getattr(config, "observability", None), "otel", None)
    if otel_cfg is None or not getattr(otel_cfg, "enabled", False):
        _tracer, _meter = None, None
        return None, None

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.debug("opentelemetry SDK not installed — in-process telemetry only")
        _tracer, _meter = None, None
        return None, None

    service_name = getattr(otel_cfg, "service_name", None) or "redibis"
    resource = Resource.create({"service.name": service_name})
    exporter_name = (getattr(otel_cfg, "exporter", None) or "file").lower()

    span_exporter = _build_span_exporter(exporter_name, otel_cfg)
    if span_exporter is None:
        _tracer, _meter = None, None
        return None, None

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("redibis")

    if getattr(otel_cfg, "metrics", True):
        try:
            metric_reader = _build_metric_reader(exporter_name, otel_cfg)
            if metric_reader is not None:
                meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
                metrics.set_meter_provider(meter_provider)
                _meter = metrics.get_meter("redibis")
            else:
                _meter = None
        except Exception as exc:
            log.debug("OTel metrics init failed: %s", exc)
            _meter = None
    else:
        _meter = None

    _register_metric_instruments(_meter)
    return _tracer, _meter


def _build_span_exporter(exporter_name: str, otel_cfg: Any) -> Any:
    try:
        if exporter_name == "otlp":
            endpoint = (getattr(otel_cfg, "otlp_endpoint", None) or "").strip()
            if not endpoint:
                return _file_span_exporter()
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            return OTLPSpanExporter(endpoint=endpoint, insecure=True)
        if exporter_name == "prometheus":
            return _file_span_exporter()
        return _file_span_exporter()
    except Exception as exc:
        log.debug("OTel span exporter %s failed (%s) — file fallback", exporter_name, exc)
        try:
            return _file_span_exporter()
        except Exception:
            return None


def _file_span_exporter() -> Any:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

    class _FileSpanExporter(SpanExporter):
        def export(self, spans) -> SpanExportResult:
            if _run_dir is None:
                return SpanExportResult.SUCCESS
            try:
                out = _run_dir / "_meta" / "telemetry" / "spans.jsonl"
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.open("a", encoding="utf-8") as fh:
                    for span in spans:
                        ctx = span.get_span_context()
                        fh.write(json.dumps({
                            "name": span.name,
                            "trace_id": format(ctx.trace_id, "032x"),
                            "span_id": format(ctx.span_id, "016x"),
                        }) + "\n")
            except Exception:
                pass
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    return _FileSpanExporter()


def _build_metric_reader(exporter_name: str, otel_cfg: Any) -> Any:
    if exporter_name == "prometheus":
        try:
            from opentelemetry.exporter.prometheus import PrometheusMetricReader

            return PrometheusMetricReader()
        except ImportError:
            return None
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    class _FileMetricExporter:
        def export(self, metrics_data, timeout_millis: int = 10_000, **kwargs) -> Any:
            if _run_dir is None:
                return None
            try:
                out = _run_dir / "_meta" / "telemetry" / "metrics.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps({"note": "metrics snapshot"}, indent=2), encoding="utf-8")
            except Exception:
                pass
            return None

        def shutdown(self, timeout_millis: int = 30_000, **kwargs) -> None:
            pass

        def force_flush(self, timeout_millis: int = 10_000) -> bool:
            return True

    return PeriodicExportingMetricReader(_FileMetricExporter(), export_interval_millis=30_000)


_METRIC_HANDLES: dict[str, Any] = {}


def _register_metric_instruments(meter: Any) -> None:
    if meter is None:
        return
    _METRIC_HANDLES["scan_duration"] = meter.create_histogram(
        "redibis.scan.duration_ms", unit="ms", description="Scan duration"
    )
    _METRIC_HANDLES["scan_count"] = meter.create_counter("redibis.scan.count")
    _METRIC_HANDLES["pii_flagged"] = meter.create_counter("redibis.pii.columns_flagged")
    _METRIC_HANDLES["model_tokens"] = meter.create_counter("redibis.model.tokens")
    _METRIC_HANDLES["model_cost"] = meter.create_counter("redibis.model.cost_usd")
    _METRIC_HANDLES["contract_upserts"] = meter.create_counter("redibis.contract.upserts")
    _METRIC_HANDLES["deep_scan_producers"] = meter.create_counter("redibis.deep_scan.producers")
    _METRIC_HANDLES["agent_duration"] = meter.create_histogram(
        "redibis.agent.run.duration_ms", unit="ms"
    )


def record_metric(name: str, value: float = 1.0, *, attributes: Optional[dict] = None) -> None:
    handle = _METRIC_HANDLES.get(name)
    if handle is None:
        return
    attrs = attributes or {}
    if hasattr(handle, "record"):
        handle.record(value, attrs)
    elif hasattr(handle, "add"):
        handle.add(value, attrs)
