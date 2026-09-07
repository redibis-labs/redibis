"""Packaged gold ODCS example for local enrichment few-shot."""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

_GOLD_PATH = Path(__file__).with_name("gold_example.yaml")


@lru_cache(maxsize=1)
def load_gold_example_contract() -> dict[str, Any]:
    """Return the built-in gold ODCS contract (deep copy)."""
    import yaml

    if not _GOLD_PATH.is_file():
        return {}
    data = yaml.safe_load(_GOLD_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(data) if isinstance(data, dict) else {}
