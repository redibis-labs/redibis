"""
Great Expectations profiler — wraps the existing ``QualityProfiler``.
"""

from __future__ import annotations

import logging

import pandas as pd

from redibis.profiling.base import ProfileResult, Profiler
from redibis.profiling.column_types import enrich_profiles_from_ge
from redibis.profiling.registry import register_profiler
from redibis.profiling.rules import expectations_to_rule_set

log = logging.getLogger(__name__)


@register_profiler("great_expectations")
class GreatExpectationsProfiler(Profiler):
    name = "great_expectations"

    def profile(self, df: pd.DataFrame, *, dataset_name: str) -> ProfileResult:
        from redibis.quality.profiler import QualityProfiler

        ge = QualityProfiler(df, dataset_name=dataset_name)
        ge.run_assistant()
        ge.profile_arabic_presence(threshold=self.config.arabic_threshold)
        triage = ge.compute_column_profiles(threshold=self.config.triage_threshold)
        triage = enrich_profiles_from_ge(triage, ge.expectations)
        ge._triage_signals = triage
        suggested = expectations_to_rule_set(ge.expectations)

        log.info(
            "GE profile complete: %d rules, %d triage signals",
            len(suggested.rules),
            len(triage),
        )

        return ProfileResult(
            column_profiles=list(triage),
            arabic_columns=dict(ge.arabic_columns),
            triage_signals=list(triage),
            suggested_rules=suggested,
            raw={"ge_profiler": ge, "engine": self.name},
        )
