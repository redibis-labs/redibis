"""Typed evidence records — leaf dataclasses with no scan/PII/store imports.

See ``docs/EVIDENCE_STORE.md`` and the unified evidence foundation plan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


SCHEMA_VERSION = "1.0"
MANIFEST_KIND = "redibis.evidence_manifest"
LLM_CALL_KIND = "redibis.llm_call"
VARIANTS_KIND = "redibis.result_variants"
COMPARISON_KIND = "redibis.result_comparison"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_call_id() -> str:
    return uuid4().hex


class CoverageStatus(str, Enum):
    EVALUATED = "evaluated"
    SKIPPED = "skipped"
    ERROR = "error"
    NOT_RUN = "not_run"


class RunOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EMPTY = "empty"


class ExecutionMode(str, Enum):
    DETERMINISTIC = "deterministic"
    SINGLE_LLM = "single_llm"
    AGENTIC = "agentic"


class SampleMode(str, Enum):
    RAW = "raw"
    MASKED = "masked"
    NONE = "none"
    REDACTED = "redacted"


def _clean(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


@dataclass
class Sensitivity:
    contains_raw_pii: bool = False
    sample_mode: str = SampleMode.NONE.value
    egress: str = "allow"
    note: str = ""
    stripped_at: str = ""
    stripped_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v or isinstance(v, bool)}


@dataclass
class CoverageEntry:
    status: str = CoverageStatus.NOT_RUN.value
    reason: str = ""
    artifact: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {"status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.artifact:
            out["artifact"] = self.artifact
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class EngineEvidenceRecord:
    """Plugin-neutral pre-verdict engine output.

    Absence of a score is not a negative verdict — ``ran=False`` plus ``reason``
    is the only way to record that an engine did not execute.
    """

    engine_id: str
    name: str = ""
    kind: str = ""
    ran: bool = False
    reason: str = ""
    score: Optional[float] = None
    entity: Optional[str] = None
    label: Optional[str] = None
    match_rate: Optional[float] = None
    hits: list[dict[str, Any]] = field(default_factory=list)
    rates: dict[str, Any] = field(default_factory=dict)
    duration_ms: Optional[float] = None
    error: str = ""
    version: str = ""
    config_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "engine_id": self.engine_id,
            "name": self.name,
            "kind": self.kind,
            "ran": bool(self.ran),
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.score is not None:
            payload["score"] = self.score
        if self.entity is not None:
            payload["entity"] = self.entity
        if self.label is not None:
            payload["label"] = self.label
        if self.match_rate is not None:
            payload["match_rate"] = self.match_rate
        if self.hits:
            payload["hits"] = list(self.hits)
        if self.rates:
            payload["rates"] = dict(self.rates)
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        if self.error:
            payload["error"] = self.error
        if self.version:
            payload["version"] = self.version
        if self.config_hash:
            payload["config_hash"] = self.config_hash
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EngineEvidenceRecord":
        data = dict(data or {})
        engine_id = str(data.pop("engine_id", data.pop("id", "")) or "")
        extra = dict(data.pop("extra", None) or {})
        known = {
            "name", "kind", "ran", "reason", "score", "entity", "label",
            "match_rate", "hits", "rates", "duration_ms", "error", "version",
            "config_hash",
        }
        for key in list(data.keys()):
            if key not in known:
                extra[key] = data.pop(key)
        return cls(engine_id=engine_id, extra=extra, **{
            k: v for k, v in data.items() if k in known
        })


@dataclass
class ArtifactRef:
    name: str
    path: str = ""
    kind: str = ""
    sensitivity: str = "shareable"
    sha256: str = ""
    size: int = 0
    local: bool = False
    storage: bool = False
    storage_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v or v == 0 or isinstance(v, bool)}


@dataclass
class LLMCallEvidence:
    """One guarded model invocation — prompts, context, config, response."""

    call_id: str = field(default_factory=new_call_id)
    seq: int = 0
    parent_call_id: str = ""
    step_id: str = ""
    attempt: int = 1
    run_id: str = ""
    table: str = ""
    agent_run_id: str = ""
    node_id: str = ""
    node_kind: str = ""
    model_role: str = ""
    execution_mode: str = ""
    provider: str = ""
    model_id: str = ""
    system_prompt: str = ""
    user_prompt: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    config_sanitized: dict[str, Any] = field(default_factory=dict)
    request_params: dict[str, Any] = field(default_factory=dict)
    response_text: str = ""
    parsed_result: Any = None
    validation: dict[str, Any] = field(default_factory=dict)
    rai: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    error: str = ""
    blocked: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    residency: str = ""
    hashes: dict[str, str] = field(default_factory=dict)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    sensitivity: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = LLM_CALL_KIND
        payload["schema_version"] = SCHEMA_VERSION
        return _clean(payload)


@dataclass
class ResultVariant:
    mode: str
    status: str = CoverageStatus.NOT_RUN.value
    reason: str = ""
    artifact: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    columns: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode, "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.artifact:
            out["artifact"] = self.artifact
        if self.summary:
            out["summary"] = dict(self.summary)
        if self.columns:
            out["columns"] = dict(self.columns)
        if self.steps:
            out["steps"] = list(self.steps)
        return out


@dataclass
class EvidenceManifest:
    table: str = ""
    run_id: str = ""
    session_id: str = ""
    schema_version: str = SCHEMA_VERSION
    kind: str = MANIFEST_KIND
    created_at: str = field(default_factory=_utc_now)
    run_status: str = ""
    coverage: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    engines: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    result_variants: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    config_hash: str = ""
    config_artifact: str = ""
    sensitivity: dict[str, Any] = field(default_factory=dict)
    source_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _clean(asdict(self))
