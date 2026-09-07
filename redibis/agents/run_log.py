"""Live log buffer + append-only audit event sink for agent runs."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

LIVE_LOG_MAX_LINES = 300

_LOCK = threading.Lock()
_BUFFERS: dict[str, deque[str]] = {}
_SUBSCRIBERS: dict[str, list[asyncio.Queue[str]]] = {}
_AUDIT_EVENTS: dict[str, list[dict[str, Any]]] = {}
_LINEAGE_ROOT: Optional[Path] = None


def configure_run_log_root(root: Path | str | None) -> None:
    """Set the agent lineage root used to persist full ``run.log`` files."""
    global _LINEAGE_ROOT
    _LINEAGE_ROOT = Path(root) if root else None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditEvent:
    """Immutable governance audit record for one node transition."""

    ts: str
    run_id: str
    table: str
    node: str
    actor: str
    inputs_hash: str
    outputs_hash: str
    status: str
    attempts: int = 1
    validation: dict[str, Any] = field(default_factory=dict)
    decision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "table": self.table,
            "node": self.node,
            "actor": self.actor,
            "inputs_hash": self.inputs_hash,
            "outputs_hash": self.outputs_hash,
            "status": self.status,
            "attempts": self.attempts,
            "validation": dict(self.validation),
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEvent":
        return cls(
            ts=str(data.get("ts") or ""),
            run_id=str(data.get("run_id") or ""),
            table=str(data.get("table") or ""),
            node=str(data.get("node") or ""),
            actor=str(data.get("actor") or "agent"),
            inputs_hash=str(data.get("inputs_hash") or ""),
            outputs_hash=str(data.get("outputs_hash") or ""),
            status=str(data.get("status") or ""),
            attempts=int(data.get("attempts") or 1),
            validation=dict(data.get("validation") or {}),
            decision=str(data.get("decision") or ""),
        )


def _append_run_log_file(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_full_run_log(lineage_root: Path, run_id: str) -> list[str]:
    """Load the persisted full log for one agent run."""
    path = Path(lineage_root) / run_id / "run.log"
    if not path.is_file():
        return []
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        return []


def append_run_log(
    run_id: str,
    line: str,
    *,
    lineage_root: Path | str | None = None,
) -> None:
    """Append one log line for an agent run and notify live SSE subscribers."""
    if not run_id:
        return
    text = str(line).rstrip()
    if not text:
        return
    root = Path(lineage_root) if lineage_root else _LINEAGE_ROOT
    with _LOCK:
        buf = _BUFFERS.setdefault(run_id, deque(maxlen=LIVE_LOG_MAX_LINES))
        buf.append(text)
        if root is not None:
            _append_run_log_file(root / run_id / "run.log", text)
        for queue in list(_SUBSCRIBERS.get(run_id, [])):
            try:
                queue.put_nowait(text)
            except Exception:
                pass


def snapshot_run_log(run_id: str) -> list[str]:
    with _LOCK:
        return list(_BUFFERS.get(run_id, ()))


def subscribe_run_log(run_id: str) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue()
    with _LOCK:
        _SUBSCRIBERS.setdefault(run_id, []).append(queue)
        for line in _BUFFERS.get(run_id, ()):
            queue.put_nowait(line)
    return queue


def unsubscribe_run_log(run_id: str, queue: asyncio.Queue[str]) -> None:
    with _LOCK:
        subs = _SUBSCRIBERS.get(run_id, [])
        if queue in subs:
            subs.remove(queue)


def drop_run_log(run_id: str) -> None:
    with _LOCK:
        _BUFFERS.pop(run_id, None)
        _SUBSCRIBERS.pop(run_id, None)
        _AUDIT_EVENTS.pop(run_id, None)


def append_audit_event(
    run_id: str,
    *,
    table: str = "",
    node: str = "",
    actor: str = "agent",
    inputs_hash: str = "",
    outputs_hash: str = "",
    status: str = "",
    attempts: int = 1,
    validation: Optional[dict[str, Any]] = None,
    decision: str = "",
) -> AuditEvent:
    """Append one immutable audit event (never edited, only appended)."""
    event = AuditEvent(
        ts=_utc_iso(),
        run_id=run_id or "",
        table=table,
        node=node,
        actor=actor,
        inputs_hash=inputs_hash,
        outputs_hash=outputs_hash,
        status=status,
        attempts=attempts,
        validation=dict(validation or {}),
        decision=decision,
    )
    if not run_id:
        return event
    with _LOCK:
        _AUDIT_EVENTS.setdefault(run_id, []).append(event.to_dict())
    append_run_log(
        run_id,
        f"AUDIT {table or '-'} · {node} · {actor} · {status} "
        f"in={inputs_hash[:8]} out={outputs_hash[:8]}",
    )
    return event


def snapshot_audit_events(run_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        return list(_AUDIT_EVENTS.get(run_id, ()))


def persist_audit_events(lineage_root: Path, run_id: str) -> Optional[Path]:
    """Flush in-memory audit events to ``audit_events.jsonl`` under the run dir."""
    events = snapshot_audit_events(run_id)
    if not run_id or not events:
        return None
    run_dir = Path(lineage_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "audit_events.jsonl"
    existing = ""
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
    lines = "".join(json.dumps(ev, default=str) + "\n" for ev in events)
    if existing:
        known = {line.strip() for line in existing.splitlines() if line.strip()}
        new_lines = [
            json.dumps(ev, default=str)
            for ev in events
            if json.dumps(ev, default=str) not in known
        ]
        if not new_lines:
            return path
        path.write_text(existing + "".join(l + "\n" for l in new_lines), encoding="utf-8")
    else:
        path.write_text(lines, encoding="utf-8")
    return path


def load_audit_events(lineage_root: Path, run_id: str) -> list[dict[str, Any]]:
    path = Path(lineage_root) / run_id / "audit_events.jsonl"
    if not path.is_file():
        return snapshot_audit_events(run_id)
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def sync_run_logs(run, *, lineage_root: Path | str | None = None) -> None:
    """Persist only the live tail in ``AgentRun.logs``; full log lives in ``run.log``."""
    root = Path(lineage_root) if lineage_root else _LINEAGE_ROOT
    full = read_full_run_log(root, run.run_id) if root is not None else []
    if not full:
        full = snapshot_run_log(run.run_id)
    run.logs = full[-LIVE_LOG_MAX_LINES:]
