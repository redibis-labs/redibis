"""
Per-run GE artifact writer for Workflow A (legacy ``QualityRunner``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import RunOutputWriter, StorageBackend

logger = logging.getLogger("redibis.quality.run_outputs")


def write_ge_run_outputs(
    df: pd.DataFrame,
    profiler: Any,
    *,
    table: str,
    run_id: str,
    output_dir: Path,
    backend: StorageBackend,
    store: ContractStore,
    runs_bucket: str = "pii-reports",
) -> dict:
    """Write GE Data Docs and quality-only ODCS partial, then upsert."""
    from redibis.quality.gatekeeper import QualityGatekeeper

    run_dir = output_dir / table.replace(".", "_") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    qa = QualityGatekeeper(
        suite_name=f"{table.replace('.', '_')}_quality_suite",
        in_memory=False,
        context_root_dir=run_dir / "ge_project",
    )
    qa.attach_dataframe(df, dataset_name=table.replace(".", "_"))
    qa.merge_expectations(profiler.expectations)
    results = qa.run_tests(stage="ge_only", generate_docs=True)

    try:
        qa.copy_data_docs_to(run_dir / "ge_report")
    except Exception as exc:
        logger.warning("Data Docs copy failed: %s", exc)

    qa.export_to_odcs(
        model_name=table.replace(".", "_"),
        output_path=str(run_dir / "data_contract.yaml"),
    )

    with open(run_dir / "data_contract.yaml") as fh:
        ge_partial = yaml.safe_load(fh)

    run_writer = RunOutputWriter(
        backend=backend,
        bucket=runs_bucket,
        workflow="ge",
        table=table,
        run_id=run_id,
    )
    run_writer.write("data_contract.yaml", ge_partial)

    ge_report_dir = run_dir / "ge_report"
    if ge_report_dir.exists():
        run_writer.write_folder("ge_report", ge_report_dir)

    upsert_result = store.upsert(
        partial=ge_partial,
        table=table,
        workflow="ge",
        run_id=run_id,
        strip_pii_quality=True,   # automated runner: no raw PII values in contract
    )

    return {
        "ge_partial": ge_partial,
        "upsert_result": upsert_result,
        "quality_summary": {
            "total":  results.statistics.get("evaluated_expectations", 0),
            "passed": results.statistics.get("successful_expectations", 0),
        },
    }
