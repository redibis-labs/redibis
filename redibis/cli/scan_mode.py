"""Deprecated shim — import from ``redibis.scan_mode`` instead."""

from __future__ import annotations

import warnings

warnings.warn(
    "redibis.cli.scan_mode is renamed to redibis.scan_mode",
    DeprecationWarning,
    stacklevel=2,
)

from redibis.scan_mode import parse_scan_mode  # noqa: F401
