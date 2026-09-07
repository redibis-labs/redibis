"""
Per-run PII artifact writer for Workflow B (legacy ``PIIDetectionRunner``).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from redibis.models import PIIDetection, RunMetadata
from redibis.pii.contract_writer import PIIContractWriter
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import RunOutputWriter, StorageBackend

logger = logging.getLogger("redibis.pii.run_outputs")


def write_pii_run_outputs(
    df: pd.DataFrame,
    detections: list[PIIDetection],
    run_metadata: RunMetadata,
    *,
    table: str,
    run_id: str,
    equation_used: str,
    output_dir: Path,
    backend: StorageBackend,
    store: ContractStore,
    runs_bucket: str = "pii-reports",
    generate_ge_docs: bool = False,
    profiler: Optional[Any] = None,
) -> dict:
    """Write per-run PII artifacts and upsert into the contract store."""
    db_name, table_name = _split_table(table)

    writer = PIIContractWriter(
        database_name=db_name,
        table_name=table_name,
        equation_used=equation_used,
        run_id=run_id,
    )
    for d in detections:
        writer.add_detection(d)
    pii_partial = writer.build()

    run_dir = output_dir / table.replace(".", "_") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if generate_ge_docs and profiler is not None:
        try:
            from redibis.quality.gatekeeper import QualityGatekeeper
            _write_ge_docs_for_pii(
                df, profiler, detections, run_metadata, run_dir, table,
                QualityGatekeeper,
            )
        except Exception as exc:
            logger.warning("GE Data Docs generation failed: %s", exc)

    run_writer = RunOutputWriter(
        backend=backend,
        bucket=runs_bucket,
        workflow="pii",
        table=table,
        run_id=run_id,
    )

    run_writer.write("data_contract.yaml", pii_partial)
    run_writer.write("pii_summary.csv", _build_pii_summary_csv(detections))
    run_writer.write("pii_detections.json", [asdict(d) for d in detections])
    run_writer.write("pii_report.json", _build_full_report(detections, equation_used))

    sample_preview_path = run_dir / "sample_preview.parquet"
    df.head(5000).to_parquet(str(sample_preview_path), index=False)
    run_writer.write("sample_preview.parquet", sample_preview_path.read_bytes())

    upsert_result = store.upsert(
        partial=pii_partial,
        table=table,
        workflow="pii",
        run_id=run_id,
        strip_pii_quality=True,   # automated runner: no raw PII values in contract
    )

    return {
        "pii_partial": pii_partial,
        "upsert_result": upsert_result,
        "detected_count": sum(1 for d in detections if d.detected),
        "total_scanned": len(detections),
    }


def _split_table(table: str) -> tuple[str, str]:
    parts = table.split(".", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("default", parts[0])


def _build_full_report(detections: list[PIIDetection], equation_used: str) -> list[dict]:
    from redibis.pii.equations import build_column_report
    return [build_column_report(d, equation=equation_used).to_dict()
            for d in detections]


def _build_pii_summary_csv(detections: list[PIIDetection]) -> str:
    def _csv_value(value: Any) -> Any:
        return "" if value is None else value

    buf = io.StringIO()
    fieldnames = [
        "column", "detected", "entity_type", "confidence", "equation_used",
        "regex_score", "ner_score", "llm_score", "phone_score",
        "msisdn_valid_rate", "phone_valid_rate", "phone_mobile_rate",
        "arabic_aware", "triage_score",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for d in detections:
        writer.writerow({
            "column":         d.column,
            "detected":       d.detected,
            "entity_type":    d.entity_type or "",
            "confidence":     d.confidence,
            "equation_used":  d.equation_used,
            "regex_score":    _csv_value(d.presidio_score),
            "ner_score":      _csv_value(d.gliner_score),
            "llm_score":      _csv_value(d.llm_score),
            "phone_score":    _csv_value(d.phone_score),
            "msisdn_valid_rate": _csv_value(d.msisdn_valid_rate),
            "phone_valid_rate": _csv_value(d.phone_valid_rate),
            "phone_mobile_rate": _csv_value(d.phone_mobile_rate),
            "arabic_aware":   d.arabic_aware,
            "triage_score":   d.triage_score,
        })
    return buf.getvalue()


def _write_ge_docs_for_pii(
    df: pd.DataFrame,
    profiler: Any,
    detections: list[PIIDetection],
    run_metadata: RunMetadata,
    run_dir: Path,
    table: str,
    gatekeeper_class: type,
) -> None:
    qa = gatekeeper_class(
        suite_name=f"{table.replace('.', '_')}_pii_suite",
        in_memory=False,
        context_root_dir=run_dir / "ge_project_pii",
    )
    qa.attach_dataframe(df, dataset_name=table.replace(".", "_"))
    if profiler is not None:
        qa.merge_expectations(profiler.expectations)
    for d in detections:
        if d.detected and d.regex_pattern:
            qa.add_pii_expectation(d)
    qa.set_run_metadata(run_metadata)
    qa.register_pii_summary(detections)
    qa.run_tests(stage="pii", generate_docs=True)
    qa.copy_data_docs_to(run_dir / "ge_report")
