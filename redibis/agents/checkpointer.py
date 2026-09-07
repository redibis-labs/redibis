"""LangGraph checkpointer factory — Postgres (prod) or Memory (dev / tests)."""

from __future__ import annotations

from typing import Any

from redibis.config import RedibisConfig

_POSTGRES_SAVER: Any = None
_POSTGRES_TRIED: bool = False
_BATCH_MEMORY: dict[str, Any] = {}


def build_checkpointer(
    cfg: RedibisConfig,
    *,
    batch_run_id: str = "",
    override: Any = None,
) -> Any:
    """Return a LangGraph checkpointer for single-table or batch runs.

    When ``memory.enabled`` and ``memory.dsn_ref`` are set, uses a process-wide
    ``PostgresSaver`` (checkpoints survive API resume across workers). Otherwise
    uses ``MemorySaver`` — batch runs get one in-process saver per ``batch_run_id``.
    """
    if override is not None:
        return override

    postgres = _postgres_saver(cfg)
    if postgres is not None:
        return postgres

    if batch_run_id:
        cp = _BATCH_MEMORY.get(batch_run_id)
        if cp is not None:
            return cp
        try:
            from langgraph.checkpoint.memory import MemorySaver

            cp = MemorySaver()
        except ImportError:
            return None
        _BATCH_MEMORY[batch_run_id] = cp
        return cp

    try:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    except ImportError:
        return None


def drop_batch_checkpointer(batch_run_id: str) -> None:
    """Release in-memory batch checkpoints (no-op for Postgres)."""
    _BATCH_MEMORY.pop(batch_run_id, None)


def _postgres_saver(cfg: RedibisConfig) -> Any | None:
    global _POSTGRES_SAVER, _POSTGRES_TRIED
    if _POSTGRES_TRIED:
        return _POSTGRES_SAVER if _POSTGRES_SAVER is not False else None

    _POSTGRES_TRIED = True
    dsn = (cfg.memory.dsn_ref or "").strip()
    if not dsn or not cfg.memory.enabled:
        return None

    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        _POSTGRES_SAVER = PostgresSaver.from_conn_string(dsn)
        return _POSTGRES_SAVER
    except Exception:
        _POSTGRES_SAVER = False
        return None
