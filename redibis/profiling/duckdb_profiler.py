"""
DuckDB SUMMARIZE profiler — fast GE-free column statistics.

Uses DuckDB's vectorized ``SUMMARIZE`` over an in-memory pandas DataFrame.
"""

from __future__ import annotations

import logging

import pandas as pd

from redibis.models import ColumnProfile
from redibis.profiling import triage
from redibis.profiling.base import ProfileResult, Profiler
from redibis.profiling.registry import register_profiler
from redibis.quality.rule_set import QualityRuleSet

log = logging.getLogger(__name__)


def _summarize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    import duckdb

    con = duckdb.connect()
    con.register("_redibis_profile_df", df)
    return con.sql("SUMMARIZE _redibis_profile_df").df()


def _null_rate(row: pd.Series, df: pd.DataFrame, col: str) -> float:
    raw = row.get("null_percentage")
    if raw is None or pd.isna(raw):
        return round(float(df[col].isna().mean()), 4)
    return round(float(raw) / 100.0, 4)


def _cardinality_ratio(row: pd.Series, row_count: int) -> float:
    approx = row.get("approx_unique")
    if approx is None or pd.isna(approx):
        return 0.0
    return round(float(approx) / max(row_count, 1), 4)


@register_profiler("duckdb")
class DuckDbProfiler(Profiler):
    """Profile a pandas DataFrame via ``duckdb.sql('SUMMARIZE …')``."""

    name = "duckdb"

    def profile(self, df: pd.DataFrame, *, dataset_name: str) -> ProfileResult:
        summary = _summarize_dataframe(df)
        row_count = max(len(df), 1)
        arabic = triage.profile_arabic_presence(df)
        triage_by_col = {
            p.column: p
            for p in triage.compute_column_profiles(
                df,
                arabic_columns=arabic,
                threshold=self.config.triage_threshold,
            )
        }

        profiles: list[ColumnProfile] = []
        for _, row in summary.iterrows():
            col = str(row["column_name"])
            triage_p = triage_by_col.get(col)
            profiles.append(
                ColumnProfile(
                    column=col,
                    dtype=str(row["column_type"]),
                    cardinality_ratio=_cardinality_ratio(row, row_count),
                    avg_value_length=triage_p.avg_value_length if triage_p else 0.0,
                    null_rate=_null_rate(row, df, col),
                    name_hint_score=triage_p.name_hint_score if triage_p else 0.0,
                    arabic_fraction=arabic.get(col, 0.0),
                    triage_score=triage_p.triage_score if triage_p else 0.0,
                    send_to_detector=triage_p.send_to_detector if triage_p else True,
                    type_source="duckdb",
                )
            )

        triage_signals = [
            p for p in profiles if p.triage_score >= self.config.triage_threshold
        ]
        log.info(
            "DuckDB profile complete: %d columns (%s)",
            len(profiles),
            dataset_name,
        )
        return ProfileResult(
            column_profiles=profiles,
            arabic_columns=arabic,
            triage_signals=triage_signals,
            suggested_rules=QualityRuleSet(),
            raw={"summarize": summary.to_dict(orient="records"), "engine": self.name},
        )
