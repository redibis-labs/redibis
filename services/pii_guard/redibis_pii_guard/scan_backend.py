"""Scan backend — library adapter over ColumnScanner / TextScanner.

Must not import mask/deid appliers (split seam for a future pii-scan service).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from redibis.pii.scan.column_scanner import ColumnScanner
from redibis.pii.scan.result import DetectionResult, TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner
from redibis.pii.rules.ruleset import RuleSet

logger = logging.getLogger("pii_guard.scan")

DEFAULT_MAX_CHARS = 50_000
DEFAULT_MAX_ROWS = 5_000
DEFAULT_MAX_COLS = 200


class ScanBackend:
    """Service-side scan adapter. Stateless per call aside from the RuleSet."""

    def __init__(self, ruleset: RuleSet, *, ner_backend: object | None = None):
        self._ruleset = ruleset
        self._ner = ner_backend
        self._text = TextScanner(ruleset=ruleset, ner_backend=ner_backend)
        self._column = ColumnScanner(ruleset=ruleset, ner_backend=ner_backend)

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    def replace_ruleset(self, ruleset: RuleSet) -> None:
        self._ruleset = ruleset
        self._text = TextScanner(ruleset=ruleset, ner_backend=self._ner)
        self._column = ColumnScanner(ruleset=ruleset, ner_backend=self._ner)

    def scan_text(
        self,
        text: str,
        *,
        language: str = "en",
        engines: str = "both",
        min_score: float = 0.35,
        resolve: str = "priority",
        return_text: bool = True,
        entities: Optional[list[str]] = None,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> DetectionResult:
        if text is None:
            raise ValueError("text is required")
        if len(text) > max_chars:
            raise ValueError(f"text exceeds max_chars ({max_chars})")
        cfg = TextScanConfig(
            engines=engines,
            language=language,
            min_score=min_score,
            return_text=return_text,
            resolve=resolve,
            max_chars=max_chars,
            entities=tuple(entities or ()),
            default_region=self._ruleset.default_region,
            arabic=(language or "").startswith("ar"),
        )
        result = self._text.scan(text, cfg)
        logger.info(
            "pii_guard_scan kind=span chars=%s entities=%s language=%s",
            result.char_count,
            dict(result.entity_counts),
            result.language,
        )
        return result

    def scan_columns(
        self,
        records: dict[str, list[Any]] | list[dict[str, Any]],
        *,
        columns: Optional[list[str]] = None,
        engines: str = "both",
        language: str = "en",
        apply_verdicts: bool = True,
        equation_mode: str = "balanced",
        max_rows: int = DEFAULT_MAX_ROWS,
        max_cols: int = DEFAULT_MAX_COLS,
    ) -> DetectionResult:
        df = _records_to_frame(records)
        if len(df.columns) > max_cols:
            raise ValueError(f"column count exceeds max_cols ({max_cols})")
        if len(df) > max_rows:
            raise ValueError(f"row count exceeds max_rows ({max_rows})")
        cols = columns or list(df.columns)
        result = self._column.scan(
            df,
            columns=cols,
            engines=engines,
            language=language,
            apply_verdicts=apply_verdicts,
            equation_mode=equation_mode,
        )
        logger.info(
            "pii_guard_scan kind=column rows=%s cols=%s entities=%s language=%s",
            len(df),
            len(cols),
            dict(result.entity_counts),
            language,
        )
        return result


def _records_to_frame(records: dict[str, list[Any]] | list[dict[str, Any]]) -> pd.DataFrame:
    if isinstance(records, dict):
        return pd.DataFrame(records)
    if isinstance(records, list):
        return pd.DataFrame(records)
    raise TypeError("records must be a column-dict or list of row dicts")
