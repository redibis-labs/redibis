"""Stdlib log formatters (JSON + plain) for redibis observability."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """One JSON object per log line — no third-party deps."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pid": os.getpid(),
            "thread": threading.get_ident(),
            "run_id": getattr(record, "run_id", ""),
            "table": getattr(record, "table", ""),
            "column": getattr(record, "column", ""),
            "fn": getattr(record, "fn", ""),
        }
        decision = getattr(record, "decision", None)
        if decision is not None:
            payload["decision"] = decision
        otel_span_id = getattr(record, "otel_span_id", None)
        if otel_span_id:
            payload["otel_span_id"] = otel_span_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
