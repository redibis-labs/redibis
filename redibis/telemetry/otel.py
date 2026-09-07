"""OpenTelemetry-style span recording for agent/tool/model calls."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from redibis.telemetry.models import ModelCallRecord, SpanRecord

_current_run: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
_current_collector: contextvars.ContextVar[Optional["TelemetryCollector"]] = contextvars.ContextVar(
    "telemetry_collector",
    default=None,
)


def get_active_collector() -> Optional["TelemetryCollector"]:
    return _current_collector.get()


def _otel_tracer():
    try:
        from redibis.telemetry.init import get_tracer
        return get_tracer()
    except ImportError:
        return None


def _otel_meter():
    try:
        from redibis.telemetry.init import get_meter
        return get_meter()
    except ImportError:
        return None


class TelemetryCollector:
    """In-process span collector with optional OTel SDK backend."""

    def __init__(self, run_id: str = "", *, otel_tracer: Any = None, otel_meter: Any = None):
        self.run_id = run_id or _current_run.get() or ""
        self._records: list[SpanRecord] = []
        self._otel_tracer = otel_tracer if otel_tracer is not None else _otel_tracer()
        self._otel_meter = otel_meter if otel_meter is not None else _otel_meter()

    @contextmanager
    def span(self, name: str, *, kind: str = "internal", **attrs: Any) -> Iterator[SpanRecord]:
        rec = SpanRecord(name=name, kind=kind, run_id=self.run_id, attributes=dict(attrs))
        otel_cm = None
        otel_span = None
        if self._otel_tracer is not None:
            try:
                otel_cm = self._otel_tracer.start_as_current_span(name)
                otel_span = otel_cm.__enter__()
                ctx = otel_span.get_span_context()
                rec.span_id = format(ctx.span_id, "016x")
            except Exception:
                otel_cm = None
                otel_span = None
        try:
            yield rec
        finally:
            rec.finish(**attrs)
            self._records.append(rec)
            if otel_cm is not None:
                try:
                    otel_cm.__exit__(None, None, None)
                except Exception:
                    pass

    def record_model_call(self, call: ModelCallRecord) -> SpanRecord:
        rec = SpanRecord(
            name="model.call",
            kind="model",
            run_id=self.run_id,
            attributes={
                "model.id": call.model_id,
                "model.provider": call.provider,
                "model.residency": call.residency,
                "model.prompt_tokens": call.prompt_tokens,
                "model.completion_tokens": call.completion_tokens,
                "model.latency_ms": call.latency_ms,
                "model.cost_usd": call.cost_usd,
                "model.blocked": call.blocked,
                "model.status": call.status,
                "model.api_base": call.api_base,
                "model.error": call.error,
            },
        )
        rec.finish()
        self._records.append(rec)
        if self._otel_meter is not None:
            try:
                from redibis.telemetry.init import record_metric

                tokens = (call.prompt_tokens or 0) + (call.completion_tokens or 0)
                record_metric("model_tokens", float(tokens), attributes={"model": call.model_id})
                if call.cost_usd:
                    record_metric("model_cost", float(call.cost_usd), attributes={"model": call.model_id})
            except Exception:
                pass
        return rec

    def export(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "kind": s.kind,
                "run_id": s.run_id,
                "span_id": s.span_id,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "attributes": dict(s.attributes),
            }
            for s in self._records
        ]


@contextmanager
def run_context(run_id: str) -> Iterator[TelemetryCollector]:
    """Isolate per-run telemetry (fixes global-logger concurrency hazard)."""
    run_token = _current_run.set(run_id)
    collector = TelemetryCollector(run_id=run_id)
    col_token = _current_collector.set(collector)
    try:
        yield collector
    finally:
        _current_collector.reset(col_token)
        _current_run.reset(run_token)
