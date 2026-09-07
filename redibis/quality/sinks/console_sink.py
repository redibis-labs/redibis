"""Console/log quality-result sink — zero-dependency default for local dev.

Also serves as the reference "second sink" proving the registry is genuinely
backend-neutral: no OpenMetadata import is reachable from this module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from redibis.quality.sinks.base import QualityResultSink, QualitySinkOptions
from redibis.quality.sinks.registry import register_quality_sink

if TYPE_CHECKING:
    from redibis.config import RedibisConfig
    from redibis.quality.schema import QualityRunV1

log = logging.getLogger("redibis.quality.monitor")


@register_quality_sink("console")
class ConsoleQualitySink(QualityResultSink):
    """Logs a one-line pass/fail summary. No network, no config required."""

    name = "console"

    @classmethod
    def from_config(cls, config: "RedibisConfig") -> "ConsoleQualitySink":
        return cls()

    def publish(
        self,
        *,
        table: str,
        contract: dict,
        run: "QualityRunV1",
        options: Optional[QualitySinkOptions] = None,
    ) -> dict[str, Any]:
        summary = run.summary or {}
        log.info(
            "monitor %s: %s (%s/%s passed) run_id=%s",
            table, run.status, summary.get("passed", 0), summary.get("total", 0), run.run_id,
        )
        return {"published": True, "summary": dict(summary)}


__all__ = ["ConsoleQualitySink"]
