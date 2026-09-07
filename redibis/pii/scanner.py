"""PII detection strategy — wraps the shared pipeline."""

from __future__ import annotations

from typing import Callable, List, Optional

import pandas as pd

from redibis.config import NERConfig, PIIConfig, RedibisConfig
from redibis.models import PIIDetection
from redibis.pii.ner_registry import NERModelRegistry
from redibis.services import pipeline
from redibis.scan.config import ScanConfig


class PIIScanner:
    def __init__(self, config: PIIConfig | ScanConfig):
        self.config = config

    def detect(
        self,
        df: pd.DataFrame,
        *,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> List[PIIDetection]:
        if isinstance(self.config, ScanConfig):
            sc = self.config
            thresholds = sc.thresholds or pipeline.thresholds_from(
                sc.pii_regex_confidence,
                sc.pii_gliner_confidence,
                sc.pii_llm_confidence,
            )
            from dataclasses import replace
            from redibis.config import resolve_pii_config

            rb = getattr(self, "_redibis_config", None)
            pii_cfg = resolve_pii_config(rb.pii if rb is not None else None)
            if sc.use_phonenumbers is not None:
                pii_cfg = replace(pii_cfg, use_phonenumbers=sc.use_phonenumbers)

            return pipeline.run_pii_detection(
                df,
                columns=sc.pii_columns,
                engines=sc.pii_engines,
                gliner_always_run=sc.pii_gliner_always_run,
                ner_always_run=sc.ner_always_run(),
                regex_overrides=sc.pii_regex_overrides,
                ner_config=sc.ner_config,
                gliner_config=sc.gliner_config,
                ner_backend=sc.ner_backend,
                equation_mode=sc.equation_mode,
                thresholds=thresholds,
                progress_callback=progress_callback,
                table=sc.table,
                pii_config=pii_cfg,
                observability_config=rb.observability if rb is not None else None,
            )

        pii = self.config
        thresholds = pii.thresholds
        regex_overrides = None
        if pii.regex_overrides:
            from redibis.pii.regex_overrides import RegexSet
            regex_overrides = RegexSet.from_dict(pii.regex_overrides).to_overrides()
        rb = getattr(self, "_redibis_config", None)
        return pipeline.run_pii_detection(
            df,
            columns=pii.selected_columns,
            engines=pii.engines,
            gliner_always_run=pii.gliner_always_run,
            ner_always_run=pii.ner_always_run(),
            regex_overrides=regex_overrides,
            ner_config=pii.ner,
            gliner_config=pii.gliner,
            equation_mode=pii.equation_mode,
            thresholds=thresholds,
            progress_callback=progress_callback,
            table=getattr(rb, "table", "") if rb else "",
            pii_config=pii,
            observability_config=rb.observability if rb else None,
        )


def pii_scanner_from_redibis(config: RedibisConfig) -> PIIScanner:
    scanner = PIIScanner(config.pii)
    scanner._redibis_config = config  # type: ignore[attr-defined]
    return scanner
