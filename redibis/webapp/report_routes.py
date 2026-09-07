"""Moved to commercial package ``redibis-reports``.

Install the add-on and import from ``redibis_reports`` instead.
See ``README.md#commercial-reporting``.
"""

from __future__ import annotations

raise ImportError(
    "PII reporting moved to the commercial add-on 'redibis-reports'. "
    "Install it (pip install redibis-reports) and import from redibis_reports.web.routes, "
    "or use redibis.reporting.get_reporting_plugin(). "
    "See README.md#commercial-reporting."
)
