"""Build PII ODCS partial from detections."""

from __future__ import annotations

from typing import List

import pandas as pd

from redibis.pii.contract_writer import PIIContractWriter
from redibis.models import PIIDetection
from redibis.scan.config import ScanConfig
from redibis.store.storage_backend import _sanitize_for_yaml


def build_pii_contract(
    detections: List[PIIDetection],
    *,
    db_name: str,
    tbl_name: str,
    config: ScanConfig,
    run_id: str,
    col_dtypes: dict,
) -> dict:
    writer = PIIContractWriter(
        database_name=db_name,
        table_name=tbl_name,
        equation_used=config.equation_mode,
        run_id=run_id,
        masking_roles=config.masking_roles,
        column_dtypes=col_dtypes,
    )
    writer.add_detections(detections)
    return _sanitize_for_yaml(writer.build())
