"""In-process change counter per contract table.

``ContractStore.upsert()`` (the only writer) bumps it after every write, so a live
view (``GET /api/contracts/{table}/events``) can notice a change within a fraction of
a second without re-reading the contract. It is a hint only: other processes (the CLI,
another web worker) don't bump it, so readers still re-read the contract now and then.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_STAMPS: dict[str, int] = {}

__all__ = ["bump", "stamp"]


def bump(table: str) -> int:
    """Record that ``table``'s contract changed; returns the new stamp."""
    with _LOCK:
        _STAMPS[table] = _STAMPS.get(table, 0) + 1
        return _STAMPS[table]


def stamp(table: str) -> int:
    """The current stamp for ``table`` (0 before any write in this process)."""
    with _LOCK:
        return _STAMPS.get(table, 0)
