"""Deprecated shim — use ``redibis.scan.contract_writer``."""

from __future__ import annotations

import warnings

from redibis.scan.contract_writer import ScanContractWriter

warnings.warn(
    "redibis.scan.contract_draft is renamed to redibis.scan.contract_writer",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["ScanContractWriter"]


def __getattr__(name: str):
    if name == "ContractDraftService":
        warnings.warn(
            "ContractDraftService is renamed to ScanContractWriter",
            DeprecationWarning,
            stacklevel=2,
        )
        return ScanContractWriter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
