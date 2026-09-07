"""Catalog backend registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from redibis.config import ConfigError
from redibis.services.catalog.atlas import AtlasPublisher
from redibis.services.catalog.base import CatalogPublisher
from redibis.services.catalog.datahub import DataHubPublisher
from redibis.services.catalog.openmetadata import OpenMetadataPublisher

if TYPE_CHECKING:
    from redibis.config import CatalogConfig

_PUBLISHERS: dict[str, type[CatalogPublisher]] = {
    "openmetadata": OpenMetadataPublisher,
    "atlas": AtlasPublisher,
    "datahub": DataHubPublisher,
}


def available_backends() -> list[str]:
    return sorted(_PUBLISHERS)


def get_publisher(config: "CatalogConfig") -> CatalogPublisher:
    name = (config.backend or "openmetadata").lower()
    cls = _PUBLISHERS.get(name)
    if cls is None:
        raise ConfigError(
            f"unknown catalog backend {config.backend!r}; choices: {available_backends()}"
        )
    return cls.from_config(config)
