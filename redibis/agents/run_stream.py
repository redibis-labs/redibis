"""SSE log stream for agent runs — mirrors the manual scan console transport."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from redibis.agents.lineage_store import LineageStore
from redibis.agents.run_log import (
    snapshot_run_log,
    subscribe_run_log,
    unsubscribe_run_log,
)
from redibis.agents.run_models import RunStatus, StepRecord


def _terminal(status: str) -> bool:
    return status in (
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    )


def _step_line(step: StepRecord) -> str:
    from redibis.agents.tool_runner import _format_step_log_line

    return _format_step_log_line(step)


def _merged_logs(run) -> list[str]:
    hub = snapshot_run_log(run.run_id)
    persisted = list(run.logs or [])
    if not hub:
        return persisted
    if not persisted:
        return hub
    if len(hub) >= len(persisted):
        return hub
    return persisted + [line for line in hub if line not in persisted]


async def agent_run_sse_generator(
    store: LineageStore,
    run_id: str,
    *,
    poll_interval: float = 0.75,
    ping_every: int = 5,
) -> AsyncGenerator[str, None]:
    """Stream captured step logs for one agent run until it reaches a terminal state."""
    seen_logs = 0
    seen_steps = 0
    idle = 0
    subscriber = None

    try:
        while True:
            if subscriber is not None:
                while True:
                    try:
                        line = subscriber.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"
                    seen_logs += 1

            try:
                run = store.load(run_id)
            except FileNotFoundError:
                yield f"data: {json.dumps({'type': 'error', 'message': 'run not found'})}\n\n"
                return

            logs = _merged_logs(run)
            for line in logs[seen_logs:]:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"
            seen_logs = len(logs)

            steps = run.steps or []
            for step in steps[seen_steps:]:
                line = _step_line(step)
                if line not in logs:
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"
            seen_steps = len(steps)

            status = run.status.value if hasattr(run.status, "value") else str(run.status)
            if _terminal(status):
                yield f"data: {json.dumps({'type': 'done', 'status': status, 'message': f'run {status}'})}\n\n"
                return

            if subscriber is None:
                subscriber = subscribe_run_log(run_id)

            idle += 1
            if idle % ping_every == 0:
                yield ": ping\n\n"

            try:
                line = await asyncio.wait_for(subscriber.get(), timeout=poll_interval)
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"
                seen_logs += 1
            except asyncio.TimeoutError:
                pass
    finally:
        if subscriber is not None:
            unsubscribe_run_log(run_id, subscriber)
