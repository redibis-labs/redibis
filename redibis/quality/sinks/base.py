"""Result-sink strategy interface — where a ``QualityRunV1`` gets published.

Adding a new destination for monitor results (Soda Cloud, DataHub assertions,
an OpenLineage event bus, ...) means implementing one class here and
registering it — ``ContinuousQualityService`` never imports a specific sink
by name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from redibis.config import RedibisConfig
    from redibis.quality.schema import QualityRunV1


@dataclass
class QualitySinkOptions:
    """Per-run context a sink may use; every field is optional so a sink can
    ignore whatever it doesn't need."""

    run_id: str = ""
    artifact_ref: str = ""
    ledger_store: Any = None


class QualityResultSink(ABC):
    """Publish one validate-only run's results to an external system.

    Implementations must be side-effect-tolerant of failure: a sink error is
    caught by the caller and never fails the underlying validation.
    """

    name: str = ""

    @classmethod
    @abstractmethod
    def from_config(cls, config: "RedibisConfig") -> "QualityResultSink":
        """Build a sink instance from the root ``RedibisConfig``."""

    @abstractmethod
    def publish(
        self,
        *,
        table: str,
        contract: dict,
        run: "QualityRunV1",
        options: Optional[QualitySinkOptions] = None,
    ) -> dict[str, Any]:
        """Publish ``run`` for ``table``. Returns a small telemetry dict."""


__all__ = ["QualityResultSink", "QualitySinkOptions"]
