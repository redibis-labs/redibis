"""Context-bound recorder that persists every guarded model call."""

from __future__ import annotations

import contextvars
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4

from redibis.evidence.models import LLMCallEvidence, new_call_id
from redibis.evidence.persist import (
    atomic_write_json,
    chmod_restricted_file,
    exclusive_file_lock,
    is_raw_artifact,
    write_llm_call_files,
)
from redibis.evidence.redact import sha256_text, shareable_llm_call
from redibis.evidence.sanitize import sanitize_mapping
from redibis.evidence.spool import llm_spool_dir, spool_root
from redibis.telemetry.models import ModelCallRecord

log = logging.getLogger(__name__)

_RECORDER: contextvars.ContextVar[Optional["LLMEvidenceRecorder"]] = contextvars.ContextVar(
    "redibis_llm_evidence_recorder",
    default=None,
)

_SHAREABLE_INDEX_KEYS = (
    "seq",
    "call_id",
    "parent_call_id",
    "model_role",
    "status",
    "blocked",
    "shareable",
    "execution_mode",
    "step_id",
    "attempt",
)


def current_llm_evidence_recorder() -> Optional["LLMEvidenceRecorder"]:
    return _RECORDER.get()


def new_llm_run_id(prefix: str) -> str:
    """Mint a unique run identity so unrelated calls never share an index."""
    stem = (prefix or "llm").strip().replace("/", "_") or "llm"
    return f"{stem}-{uuid4().hex[:12]}"


def resolve_spool_dir(config: Any = None) -> Path:
    """Governed local spool for exact restricted LLM records."""
    ev = getattr(config, "evidence", None) if config is not None else None
    if ev is not None and getattr(ev, "restricted_spool_dir", None):
        return Path(ev.restricted_spool_dir)
    report = getattr(config, "report", None) if config is not None else None
    if report is not None and getattr(report, "output_dir", None):
        return Path(report.output_dir) / "_restricted_evidence"
    output_dir = getattr(config, "output_dir", None) if config is not None else None
    if output_dir is not None:
        return Path(output_dir) / "_restricted_evidence"
    try:
        from redibis.config import RedibisConfig

        cfg = RedibisConfig.load()
        return Path(cfg.evidence.restricted_spool_dir)
    except Exception:
        return spool_root(None)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _shareable_index_payload(recorder: "LLMEvidenceRecorder") -> dict[str, Any]:
    calls = []
    for entry in recorder.index:
        item = {k: entry.get(k) for k in _SHAREABLE_INDEX_KEYS}
        item["execution_mode"] = item.get("execution_mode") or recorder.execution_mode
        item["shareable"] = str(item.get("shareable") or "")
        calls.append(item)
    return {"calls": calls, "warnings": list(recorder.warnings)}


def _full_index_payload(recorder: "LLMEvidenceRecorder") -> dict[str, Any]:
    return {"calls": list(recorder.index), "warnings": list(recorder.warnings)}


def _lock_path(recorder: "LLMEvidenceRecorder") -> Optional[Path]:
    rid = recorder.run_id or "unbound"
    if recorder.spool_dir is not None:
        return llm_spool_dir(recorder.spool_dir, rid) / "index.json.lock"
    if recorder.run_dir is not None:
        return Path(recorder.run_dir) / "llm_calls" / "index.json.lock"
    return None


@dataclass
class LLMEvidenceRecorder:
    run_dir: Optional[Path] = None
    run_id: str = ""
    table: str = ""
    agent_run_id: str = ""
    execution_mode: str = ""
    run_writer: Any = None
    config_sanitized: dict[str, Any] = field(default_factory=dict)
    spool_dir: Optional[Path] = None
    seq: int = 0
    index: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def warn(self, phase: str, error: str) -> None:
        entry = {"phase": phase, "error": str(error)}
        self.warnings.append(entry)
        log.warning("LLM evidence %s: %s", phase, error)

    def record(self, evidence: LLMCallEvidence) -> dict[str, Any]:
        lock = _lock_path(self)
        if lock is not None:
            with exclusive_file_lock(lock):
                return self._record_locked(evidence)
        return self._record_locked(evidence)

    def _record_locked(self, evidence: LLMCallEvidence) -> dict[str, Any]:
        _hydrate_index(self)
        self.seq += 1
        evidence.seq = self.seq
        evidence.run_id = evidence.run_id or self.run_id
        evidence.table = evidence.table or self.table
        evidence.agent_run_id = evidence.agent_run_id or self.agent_run_id
        evidence.execution_mode = evidence.execution_mode or self.execution_mode
        if not evidence.config_sanitized and self.config_sanitized:
            evidence.config_sanitized = dict(self.config_sanitized)
        payload = evidence.to_dict()
        paths: dict[str, str] = {}
        try:
            paths = write_llm_call_files(
                Path(self.run_dir) if self.run_dir is not None else None,
                payload,
                seq=self.seq,
                spool_dir=self.spool_dir,
                run_id=evidence.run_id or self.run_id,
            )
        except Exception as exc:
            self.warn("persist", f"{type(exc).__name__}: {exc}")
        shareable = shareable_llm_call(payload)
        if self.run_writer is not None and paths.get("rel_shareable"):
            try:
                self.run_writer.write(paths["rel_shareable"], shareable)
            except Exception as exc:
                self.warn("upload", f"{type(exc).__name__}: {exc}")
        elif self.run_writer is not None:
            name = f"llm_calls/{self.seq:04d}-{evidence.call_id}.json"
            try:
                self.run_writer.write(name, shareable)
                paths["rel_shareable"] = name
            except Exception as exc:
                self.warn("upload", f"{type(exc).__name__}: {exc}")
        entry = {
            "seq": self.seq,
            "call_id": evidence.call_id,
            "parent_call_id": evidence.parent_call_id,
            "model_role": evidence.model_role,
            "status": evidence.status,
            "blocked": evidence.blocked,
            "shareable": paths.get("rel_shareable") or paths.get("shareable") or "",
            "raw": paths.get("rel_raw") or "",
            "spool": paths.get("spool_raw") or "",
            "execution_mode": evidence.execution_mode or self.execution_mode,
            "step_id": evidence.step_id,
            "attempt": evidence.attempt,
        }
        self.index.append(entry)
        _write_index(self)
        return entry


@contextmanager
def llm_evidence_recorder(
    *,
    run_dir: Optional[Path] = None,
    run_id: str = "",
    table: str = "",
    agent_run_id: str = "",
    execution_mode: str = "",
    run_writer: Any = None,
    config_sanitized: Optional[dict[str, Any]] = None,
    spool_dir: Optional[Path] = None,
    config: Any = None,
) -> Iterator[LLMEvidenceRecorder]:
    existing = _RECORDER.get()
    if existing is not None:
        yield existing
        return
    resolved_spool = Path(spool_dir) if spool_dir is not None else resolve_spool_dir(config)
    recorder = LLMEvidenceRecorder(
        run_dir=Path(run_dir) if run_dir is not None else None,
        run_id=run_id,
        table=table,
        agent_run_id=agent_run_id,
        execution_mode=execution_mode,
        run_writer=run_writer,
        config_sanitized=dict(config_sanitized or {}),
        spool_dir=resolved_spool,
    )
    lock = _lock_path(recorder)
    if lock is not None:
        with exclusive_file_lock(lock):
            _hydrate_index(recorder)
    else:
        _hydrate_index(recorder)
    token = _RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _RECORDER.reset(token)
        if lock is not None:
            with exclusive_file_lock(lock):
                _write_index(recorder)
        else:
            _write_index(recorder)


def persist_guarded_call(
    *,
    model_id: str,
    provider: Any,
    system_prompt: str = "",
    user_prompt: str = "",
    response_text: str = "",
    parsed_result: Any = None,
    rai_report: Optional[dict[str, Any]] = None,
    status: str = "ok",
    error: str = "",
    blocked: bool = False,
    metrics: Optional[ModelCallRecord] = None,
    context: Optional[dict[str, Any]] = None,
    request_params: Optional[dict[str, Any]] = None,
    model_role: str = "",
    run_id: str = "",
    table: str = "",
    parent_call_id: str = "",
    step_id: str = "",
    attempt: int = 1,
    node_id: str = "",
    node_kind: str = "",
    validation: Optional[dict[str, Any]] = None,
    omitted: Optional[list[dict[str, Any]]] = None,
    execution_mode: str = "",
) -> Optional[str]:
    """Persist one call. Bound recorder preferred; otherwise spool by run identity."""
    recorder = current_llm_evidence_recorder()
    rec = metrics
    if recorder is None:
        rid = run_id or (rec.run_id if rec else "") or f"{model_role or 'llm'}-{_utc_stamp()}"
        recorder = LLMEvidenceRecorder(
            run_dir=None,
            run_id=rid,
            table=table,
            execution_mode=execution_mode,
            spool_dir=resolve_spool_dir(),
        )
        _hydrate_index(recorder)
    provider_name = ""
    if provider is not None:
        provider_name = str(getattr(provider, "name", "") or "")
    evidence = LLMCallEvidence(
        call_id=new_call_id(),
        parent_call_id=parent_call_id,
        step_id=step_id,
        attempt=attempt,
        run_id=run_id or (rec.run_id if rec else "") or recorder.run_id,
        table=table,
        node_id=node_id,
        node_kind=node_kind,
        model_role=model_role or (rec.model_role if rec else ""),
        execution_mode=execution_mode or recorder.execution_mode,
        provider=provider_name or (rec.provider if rec else ""),
        model_id=model_id or (rec.model_id if rec else ""),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        context=sanitize_mapping(context or {}),
        request_params=sanitize_mapping(request_params or {}),
        response_text=response_text,
        parsed_result=parsed_result,
        validation=dict(validation or {}),
        rai=dict(rai_report or {}),
        status=status,
        error=error,
        blocked=blocked,
        prompt_tokens=int(rec.prompt_tokens) if rec else 0,
        completion_tokens=int(rec.completion_tokens) if rec else 0,
        latency_ms=float(rec.latency_ms) if rec else 0.0,
        cost_usd=float(rec.cost_usd) if rec else 0.0,
        residency=(rec.residency if rec else "") or str(getattr(provider, "residency", "") or ""),
        hashes={
            "system_prompt": sha256_text(system_prompt)[:16],
            "user_prompt": sha256_text(user_prompt)[:16],
            "response": sha256_text(response_text)[:16],
        },
        omitted=list(omitted or []),
    )
    recorder.record(evidence)
    return evidence.call_id


def persist_restricted_artifact(
    run_id: str,
    name: str,
    payload: Any,
    *,
    spool_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Write an exact artifact into the governed spool (never the runs bucket)."""
    if not run_id:
        return None
    dest = llm_spool_dir(spool_dir or resolve_spool_dir(), run_id).parent / name
    try:
        from redibis.evidence.persist import ensure_restricted_dir

        ensure_restricted_dir(dest.parent)
        if isinstance(payload, (bytes, bytearray)):
            dest.write_bytes(payload)
        else:
            atomic_write_json(dest, payload, restricted=True)
        chmod_restricted_file(dest)
        return dest
    except Exception as exc:
        log.warning("restricted artifact persist failed run_id=%s name=%s: %s", run_id, name, exc)
        rec = current_llm_evidence_recorder()
        if rec is not None:
            rec.warn("restricted_artifact", f"{name}: {exc}")
        return None


def skip_raw_upload(name: str) -> bool:
    return is_raw_artifact(name)


def _hydrate_index(recorder: LLMEvidenceRecorder) -> None:
    """Continue sequence numbers across resume / re-entry."""
    paths: list[Path] = []
    rid = recorder.run_id
    if recorder.spool_dir is not None and rid:
        paths.append(llm_spool_dir(recorder.spool_dir, rid) / "index.json")
    if recorder.run_dir is not None:
        paths.append(Path(recorder.run_dir) / "llm_calls" / "index.json")
    loaded: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            continue
        calls = list(data.get("calls") or [])
        if calls:
            loaded = calls
            warnings = list(data.get("warnings") or [])
            break
    if not loaded:
        return
    seen = {str(c.get("call_id") or "") for c in recorder.index}
    for entry in loaded:
        cid = str(entry.get("call_id") or "")
        if cid and cid in seen:
            continue
        recorder.index.append(entry)
        seen.add(cid)
    recorder.warnings = list(warnings) + list(recorder.warnings)
    max_seq = max((int(c.get("seq") or 0) for c in recorder.index), default=0)
    if max_seq > recorder.seq:
        recorder.seq = max_seq


def _write_index(recorder: LLMEvidenceRecorder) -> None:
    _hydrate_index(recorder)
    rid = recorder.run_id
    if recorder.run_dir is not None:
        share_path = Path(recorder.run_dir) / "llm_calls" / "index.json"
        try:
            atomic_write_json(share_path, _shareable_index_payload(recorder))
        except OSError as exc:
            recorder.warn("index", f"{share_path}: {exc}")
    if recorder.spool_dir is not None and rid:
        full_path = llm_spool_dir(recorder.spool_dir, rid) / "index.json"
        try:
            atomic_write_json(full_path, _full_index_payload(recorder), restricted=True)
        except OSError as exc:
            recorder.warn("index", f"{full_path}: {exc}")
