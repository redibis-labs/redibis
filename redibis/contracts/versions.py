"""ODCS version profiles for Redibis.

Existing scan/enrich writers continue to emit the project default
(``v3.0.1``). Contract Synthesis validates and exports portable
``v3.1.0`` candidates without writing through ``ContractStore.upsert``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Optional


# Canonical version emitted by existing Redibis writers (unchanged).
DEFAULT_EMITTED_API_VERSION = "v3.0.1"

# Synthesis / portable export target.
SYNTHESIS_API_VERSION = "v3.1.0"

# Reserved until Bitol finalizes the schema.
FUTURE_API_VERSION = "v3.2.0"

SUPPORTED_READ_API_VERSIONS = frozenset({
    "v3.0.0",
    "v3.0.1",
    "v3.0.2",
    "v3.1.0",
})


@dataclass(frozen=True)
class OdcsVersionProfile:
    """Exact ODCS apiVersion profile."""

    api_version: str
    schema_resource: str
    enabled: bool = True
    notes: str = ""

    def schema_path(self) -> Path:
        """Resolve the bundled official JSON Schema for this profile."""
        if not self.enabled:
            raise RuntimeError(
                f"ODCS {self.api_version} profile is disabled: {self.notes or 'not ready'}"
            )
        # Prefer package data under redibis.contracts.schemas
        try:
            root = resources.files("redibis.contracts.schemas")
            candidate = root.joinpath(self.schema_resource)
            if candidate.is_file():
                return Path(str(candidate))
        except (TypeError, FileNotFoundError, ModuleNotFoundError):
            pass
        # Repo checkout fallback
        here = Path(__file__).resolve().parent
        for path in (
            here / "schemas" / self.schema_resource,
            here.parent.parent / "schemas" / "odcs" / self.schema_resource,
        ):
            if path.is_file():
                return path
        raise FileNotFoundError(
            f"ODCS schema not found for {self.api_version}: {self.schema_resource}"
        )


PROFILES: dict[str, OdcsVersionProfile] = {
    "v3.0.1": OdcsVersionProfile(
        api_version="v3.0.1",
        schema_resource="odcs-json-schema-v3.1.0.json",  # 3.1 schema accepts 3.0.x enums
        enabled=True,
        notes="Legacy Redibis emit target; validated against compatible schema.",
    ),
    "v3.0.2": OdcsVersionProfile(
        api_version="v3.0.2",
        schema_resource="odcs-json-schema-v3.1.0.json",
        enabled=True,
    ),
    "v3.1.0": OdcsVersionProfile(
        api_version="v3.1.0",
        schema_resource="odcs-json-schema-v3.1.0.json",
        enabled=True,
        notes="Contract Synthesis export target (native relationships).",
    ),
    "v3.2.0": OdcsVersionProfile(
        api_version="v3.2.0",
        schema_resource="",
        enabled=False,
        notes="Disabled until Bitol publishes the finalized ODCS v3.2.0 schema.",
    ),
}


def get_profile(api_version: str) -> OdcsVersionProfile:
    key = (api_version or "").strip()
    if key not in PROFILES:
        raise ValueError(
            f"unsupported ODCS apiVersion {api_version!r}; "
            f"known: {sorted(PROFILES)}"
        )
    return PROFILES[key]


def normalize_api_version(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text:
        return DEFAULT_EMITTED_API_VERSION
    return text


def is_v3_family(api_version: str) -> bool:
    return bool(api_version) and api_version.startswith("v3.")
