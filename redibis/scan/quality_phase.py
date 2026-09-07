"""Quality validation phase — gatekeeper + contract partial."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import pandas as pd

from redibis.contracts.type_inference import dtype_map_from_dataframe
from redibis.profiling.base import ProfileResult
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.quality.rule_set import QualityRuleSet
from redibis.quality.validator import GreatExpectationsValidator, validator_for_scan
from redibis.scan.config import ScanConfig


def extract_quality_stats(results: Any) -> dict:
    try:
        for _key, val in results.run_results.items():
            if hasattr(val, "to_json_dict"):
                vr = val.to_json_dict()
            elif isinstance(val, dict):
                vr = val.get("validation_result", val)
            else:
                continue
            stats = vr.get("statistics", {})
            return {
                "total": stats.get("evaluated_expectations", 0),
                "passed": stats.get("successful_expectations", 0),
                "failed": stats.get("unsuccessful_expectations", 0),
            }
    except Exception:
        pass
    return {}


def run_quality_phase(
    df: pd.DataFrame,
    config: ScanConfig,
    profile: ProfileResult,
    *,
    db_name: str,
    tbl_name: str,
    run_dir: Path,
    rule_set: Optional[QualityRuleSet] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Tuple[dict, QualityGatekeeper, Any, dict]:
    """Run GE validation and build the quality ODCS partial."""
    if log_fn:
        log_fn("Running quality gatekeeper...")
    validator = validator_for_scan(config, tbl_name=tbl_name, run_dir=run_dir)
    validator.attach_dataframe(df, dataset_name=tbl_name)
    validator.apply_rules(rule_set or QualityRuleSet(), profile)
    quality_results = validator.run_tests(stage="scan", generate_docs=config.generate_ge_docs)
    col_dtypes = dtype_map_from_dataframe(df)
    quality_contract = validator.export_quality_contract(
        database_name=db_name,
        table_name=tbl_name,
        column_dtypes=col_dtypes,
    )
    qa = validator.gatekeeper if isinstance(validator, GreatExpectationsValidator) else None
    return quality_contract, qa, quality_results, extract_quality_stats(quality_results)
