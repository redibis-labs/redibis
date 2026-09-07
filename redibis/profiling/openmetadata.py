"""
OpenMetadata profiler — pandas-native metrics with OM-compatible output.

No ``openmetadata-ingestion`` dependency is required; metrics are computed
directly on the DataFrame (ARCHITECTURE_PLAN §4 option b).
"""

from __future__ import annotations

import logging

import pandas as pd

from redibis.profiling.base import ProfileResult, Profiler
from redibis.profiling.column_types import enrich_profiles_from_openmetadata
from redibis.profiling import triage
from redibis.profiling.om_metrics import (
    columns_suppress_type_coercion,
    compute_column_metrics,
    metrics_to_type_hints,
)
from redibis.profiling.om_report import render_metrics_report
from redibis.profiling.om_rules import metrics_to_rule_set
from redibis.profiling.registry import register_profiler

log = logging.getLogger(__name__)


@register_profiler("open_metadata")
class OpenMetadataProfiler(Profiler):
    name = "open_metadata"

    def profile(self, df: pd.DataFrame, *, dataset_name: str) -> ProfileResult:
        om_cfg = self.config.openmetadata
        metrics = compute_column_metrics(
            df,
            max_frequent_values=om_cfg.max_frequent_values,
        )
        arabic = triage.profile_arabic_presence(df)
        profiles = triage.compute_column_profiles(
            df,
            arabic_columns=arabic,
            threshold=self.config.triage_threshold,
        )
        type_hints = metrics_to_type_hints(
            metrics,
            suppress_coercion_for=columns_suppress_type_coercion(profiles),
        )
        profiles = enrich_profiles_from_openmetadata(profiles, type_hints)
        suggested = metrics_to_rule_set(
            metrics,
            max_frequent_values=om_cfg.max_frequent_values,
        )
        native_html = render_metrics_report(metrics, dataset_name=dataset_name)

        log.info(
            "OpenMetadata profile complete: %d metrics, %d rules, %d triage signals",
            len(metrics),
            len(suggested.rules),
            len(profiles),
        )

        return ProfileResult(
            column_profiles=list(profiles),
            arabic_columns=arabic,
            triage_signals=list(profiles),
            suggested_rules=suggested,
            native_report_html=native_html,
            raw={"om_metrics": metrics, "engine": self.name},
        )
