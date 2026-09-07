"""Deprecated shim — import from ``redibis.runner_utils`` instead."""

from __future__ import annotations

import warnings

warnings.warn(
    "redibis.cli.utils is renamed to redibis.runner_utils",
    DeprecationWarning,
    stacklevel=2,
)

from redibis.runner_utils import (  # noqa: F401, E402
    StepTiming,
    load_sample,
    timed_step,
    utc_iso,
)
