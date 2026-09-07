"""Telemetry dataclasses for agent runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SpanRecord:
    name: str
    kind: str = "internal"
    run_id: str = ""
    parent_id: str = ""
    span_id: str = field(default_factory=lambda: uuid4().hex[:16])
    start_time: str = field(default_factory=_utc_now)
    end_time: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def finish(self, **attrs: Any) -> None:
        self.end_time = _utc_now()
        self.attributes.update(attrs)


@dataclass
class ModelCallRecord:
    model_id: str
    provider: str = "local"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    residency: str = "local"
    decision: str = ""
    blocked: bool = False
    block_reason: str = ""
    status: str = "ok"
    api_base: str = ""
    error: str = ""
    system_prompt_len: int = 0
    user_prompt_len: int = 0
    model_role: str = ""
    routing_revision: int = 0
    run_id: str = ""
    timestamp: str = field(default_factory=_utc_now)
