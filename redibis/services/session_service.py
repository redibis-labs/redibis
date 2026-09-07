"""
redibis.services.session_service
================================
Façade re-exporting the web session layer.

Implementation lives in ``redibis.services.session.*``; this module keeps the
frozen import paths required by ``webapp/backend.py`` (UI_SAFETY_CHECKLIST).
"""

from __future__ import annotations

from redibis.services.session.approved import (
    build_approved_partials,
    merge_approved,
    pii_row_to_fragment,
    quality_row_to_fragment,
)
from redibis.services.session.config import (
    CommonConfig,
    GlobalConfig,
    build_subcontract_store,
    from_global_config,
    to_global_config,
)
from redibis.services.session.state import (
    ApprovedProperty,
    ApprovedSet,
    RunRecord,
    ScanSession,
    SessionManager,
    SubContract,
    _load_dataframe,
    load_session_from_dir,
    rehydrate_scan_session,
    session_manager,
    sse_log_generator,
    split_table,
)
from redibis.services.session.steps import (
    execute_discovery_run,
    execute_pii_step,
    execute_profile_step,
    execute_quality_step,
    execute_unified_scan,
    merge_session_sub_contracts,
)

# Test/helper access (not part of webapp frozen surface).
from redibis.services.session.config import _build_scan_config  # noqa: F401

__all__ = [
    "ApprovedProperty",
    "ApprovedSet",
    "CommonConfig",
    "GlobalConfig",
    "RunRecord",
    "ScanSession",
    "SessionManager",
    "SubContract",
    "_load_dataframe",
    "build_approved_partials",
    "build_subcontract_store",
    "execute_discovery_run",
    "execute_pii_step",
    "execute_profile_step",
    "execute_quality_step",
    "execute_unified_scan",
    "from_global_config",
    "load_session_from_dir",
    "merge_approved",
    "merge_session_sub_contracts",
    "pii_row_to_fragment",
    "quality_row_to_fragment",
    "rehydrate_scan_session",
    "session_manager",
    "split_table",
    "sse_log_generator",
    "to_global_config",
]
