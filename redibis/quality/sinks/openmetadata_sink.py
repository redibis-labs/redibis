"""OpenMetadata quality-result sink — wraps the existing OM test-case publisher."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from redibis.quality.sinks.base import QualityResultSink, QualitySinkOptions
from redibis.quality.sinks.registry import register_quality_sink

if TYPE_CHECKING:
    from redibis.config import RedibisConfig
    from redibis.quality.schema import QualityRunV1


@register_quality_sink("openmetadata")
class OpenMetadataQualitySink(QualityResultSink):
    """Publishes ``TestSuite`` / ``TestCase`` / ``TestCaseResult`` to OpenMetadata.

    This is the same ``publish_quality()`` used by ``catalog push`` replay —
    stable test identities, ledger ownership, and graceful degradation on
    older OM servers are unchanged; only the call site moved behind the
    ``QualityResultSink`` strategy interface.
    """

    name = "openmetadata"

    def __init__(self, config: "RedibisConfig") -> None:
        self.config = config

    @classmethod
    def from_config(cls, config: "RedibisConfig") -> "OpenMetadataQualitySink":
        return cls(config)

    def publish(
        self,
        *,
        table: str,
        contract: dict,
        run: "QualityRunV1",
        options: Optional[QualitySinkOptions] = None,
    ) -> dict[str, Any]:
        from redibis.services.catalog.openmetadata import (
            OpenMetadataPublisher,
            _OpenMetadataClient,
        )
        from redibis.services.catalog.openmetadata_quality import (
            map_results_for_publish,
            publish_quality,
        )

        opts = options or QualitySinkOptions()
        publisher = OpenMetadataPublisher.from_config(self.config.catalog)
        client = _OpenMetadataClient.from_settings(publisher.settings)
        table_fqn = publisher.managed_table_fqn(table)
        mapped = map_results_for_publish(run.to_dict())
        started_at = None
        if run.started_at:
            try:
                started_at = datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
            except ValueError:
                started_at = None
        return publish_quality(
            client,
            contract,
            table,
            table_fqn=table_fqn,
            results=mapped,
            run_started_at=started_at,
            ledger_store=getattr(publisher, "ledger_store", None),
            artifact_ref=opts.artifact_ref,
            run_id=opts.run_id or run.run_id,
        )


__all__ = ["OpenMetadataQualitySink"]
