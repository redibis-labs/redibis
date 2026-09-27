"""Row filtering shared by stores that read whole files/partitions into pandas."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

import pandas as pd


def apply(df: pd.DataFrame, *, table_name: str, equals: Optional[dict[str, Any]] = None,
          isin: Optional[dict[str, Iterable[Any]]] = None,
          date_from: Optional[date] = None, date_to: Optional[date] = None) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df["table_name"] == table_name
    for col, value in (equals or {}).items():
        mask &= df[col] == value
    for col, values in (isin or {}).items():
        mask &= df[col].isin(list(values))
    if date_from is not None or date_to is not None:
        dates = pd.to_datetime(df["partition_date"], errors="coerce")
        if date_from is not None:
            mask &= dates >= pd.Timestamp(date_from)
        if date_to is not None:
            mask &= dates <= pd.Timestamp(date_to)
    return df[mask].reset_index(drop=True)
