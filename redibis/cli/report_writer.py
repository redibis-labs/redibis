"""Deprecated shim — import from ``redibis.pii.run_outputs`` / ``redibis.quality.run_outputs``."""

from __future__ import annotations

import warnings

warnings.warn(
    "redibis.cli.report_writer is renamed to redibis.pii.run_outputs / redibis.quality.run_outputs",
    DeprecationWarning,
    stacklevel=2,
)

from redibis.pii.run_outputs import write_pii_run_outputs  # noqa: F401, E402
from redibis.quality.run_outputs import write_ge_run_outputs  # noqa: F401, E402
