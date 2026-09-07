"""Open-core seam for the commercial PII reporting add-on.

Discovery uses setuptools entry points::

    [project.entry-points."redibis.reporting"]
    reports = "redibis_reports.plugin:factory"

Without a registered plugin, web/CLI surfaces show an install hint.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class ReportingPluginProtocol(Protocol):
    name: str
    version: str

    def register_web(self, app: Any, **kwargs: Any) -> dict[str, Any]: ...

    def run_cli(self, args: Any) -> int: ...

    def create_service(self, **kwargs: Any) -> Any: ...


class ReportingAddonMissing(RuntimeError):
    """Raised when commercial reporting is required but not installed."""

    def __init__(self, detail: str = "") -> None:
        msg = (
            "PII reporting is provided by the commercial add-on "
            "'redibis-reports'. Install it into this environment "
            "(see README.md#commercial-reporting), then restart the webapp."
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


def get_reporting_plugin() -> Optional[ReportingPluginProtocol]:
    """Return the first registered ``redibis.reporting`` plugin, or ``None``."""
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        group = (
            eps.select(group="redibis.reporting")
            if hasattr(eps, "select")
            else eps.get("redibis.reporting", [])  # type: ignore[arg-type]
        )
        for ep in group:
            try:
                factory = ep.load()
                plugin = factory() if callable(factory) else factory
                if plugin is not None:
                    return plugin
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def reporting_available() -> bool:
    return get_reporting_plugin() is not None


def register_reporting(app: Any, **kwargs: Any) -> Optional[dict[str, Any]]:
    """Mount reporting routes/UI when the add-on is installed.

    Returns registration metadata, or ``None`` when the add-on is absent
    (caller should serve the upsell page).
    """
    plugin = get_reporting_plugin()
    if plugin is None:
        return None
    return plugin.register_web(app, **kwargs)


def run_reporting_cli(args: Any) -> int:
    """Dispatch ``redibis report …`` to the add-on, or print an install hint."""
    plugin = get_reporting_plugin()
    if plugin is None:
        import sys

        print(str(ReportingAddonMissing()), file=sys.stderr)
        return 2
    return plugin.run_cli(args)
