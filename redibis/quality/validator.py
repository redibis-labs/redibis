"""Quality validation strategy — ABC seam over the GE gatekeeper."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import pandas as pd

from redibis.config import ConfigError
from redibis.profiling.base import ProfileResult
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.quality.rule_set import QualityRuleSet
from redibis.quality.registry import register_validator
from redibis.services import pipeline

if TYPE_CHECKING:
    from redibis.scan.config import ScanConfig


class Validator(ABC):
    """Run quality expectations against a DataFrame and export an ODCS partial."""

    @abstractmethod
    def attach_dataframe(self, df: pd.DataFrame, *, dataset_name: str) -> None:
        ...

    @abstractmethod
    def apply_rules(
        self,
        rule_set: QualityRuleSet,
        profile: ProfileResult,
    ) -> None:
        ...

    @abstractmethod
    def run_tests(self, *, stage: str, generate_docs: bool) -> Any:
        ...

    @abstractmethod
    def export_quality_contract(
        self,
        *,
        database_name: str,
        table_name: str,
        column_dtypes: dict,
    ) -> dict:
        ...


@register_validator("great_expectations")
class GreatExpectationsValidator(Validator):
    """GE-backed validator — wraps ``QualityGatekeeper``."""

    def __init__(
        self,
        *,
        suite_name: str,
        in_memory: bool,
        context_root_dir: Path,
    ) -> None:
        self._gk = QualityGatekeeper(
            suite_name=suite_name,
            in_memory=in_memory,
            context_root_dir=context_root_dir,
        )

    @property
    def gatekeeper(self) -> QualityGatekeeper:
        return self._gk

    def attach_dataframe(self, df: pd.DataFrame, *, dataset_name: str) -> None:
        self._gk.attach_dataframe(df, dataset_name=dataset_name)

    def apply_rules(
        self,
        rule_set: QualityRuleSet,
        profile: ProfileResult,
    ) -> None:
        effective = rule_set
        if not effective or not effective.rules:
            effective = profile.suggested_rules
        pipeline.apply_quality_rules(self._gk, effective, profile.expectations)

    def run_tests(self, *, stage: str, generate_docs: bool) -> Any:
        return self._gk.run_tests(stage=stage, generate_docs=generate_docs)

    def export_quality_contract(
        self,
        *,
        database_name: str,
        table_name: str,
        column_dtypes: dict,
    ) -> dict:
        return self._gk.export_quality_contract(
            database_name=database_name,
            table_name=table_name,
            output_path=None,
            column_dtypes=column_dtypes,
        )


def validator_for_scan(
    config: ScanConfig,
    *,
    tbl_name: str,
    run_dir: Path,
) -> Validator:
    """Return the registered validator for a scan (GE default today)."""
    from redibis.quality.registry import get_validator_class

    engine = getattr(config, "validator_engine", None) or "great_expectations"
    cls = get_validator_class(engine)
    if cls is GreatExpectationsValidator:
        return cls(
            suite_name=f"{tbl_name}_scan_suite",
            in_memory=not config.generate_ge_docs,
            context_root_dir=run_dir / "ge_project",
        )
    raise ConfigError(
        f"validator {engine!r} is registered but has no scan factory; "
        "extend validator_for_scan()"
    )
