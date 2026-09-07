"""Deprecated shim — import from ``redibis.pipeline_config`` instead."""

from __future__ import annotations

import warnings

warnings.warn(
    "redibis.cli.legacy_config is renamed to redibis.pipeline_config",
    DeprecationWarning,
    stacklevel=2,
)

from redibis.pipeline_config import (  # noqa: F401, E402
    GEPipelineConfig,
    PipelineConfig,
    to_ge_pipeline_config,
    to_pipeline_config,
)
