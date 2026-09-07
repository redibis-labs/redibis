"""Typed models for portable Redibis Pack manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PackMetadata(_Strict):
    id: str
    version: str
    uuid: Optional[str] = None
    family_id: Optional[str] = None
    parent_uuid: Optional[str] = None
    description: str = ""
    author: str = ""
    created: Optional[str] = None
    base: Optional[str] = None
    tenant: Optional[str] = None

    @field_validator("id", "version")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        value = (v or "").strip()
        if not value:
            raise ValueError("must be non-empty")
        if "/" in value or "\\" in value:
            raise ValueError("must not contain path separators")
        return value

    @field_validator("uuid", "family_id", "parent_uuid", mode="before")
    @classmethod
    def _optional_identity(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        value = str(v).strip()
        if not value:
            return None
        if "/" in value or "\\" in value:
            raise ValueError("must not contain path separators")
        return value


class PackRegistries(_Strict):
    validators: list[str] = Field(default_factory=list)
    behavior_actions: list[str] = Field(default_factory=list)
    ner_backends: list[str] = Field(default_factory=list)
    profilers: list[str] = Field(default_factory=list)


class PackRequires(_Strict):
    redibis: str = ">=0"
    registries: PackRegistries = Field(default_factory=PackRegistries)


class PackContents(_Strict):
    config: bool = False
    locale: bool = False
    behavior: list[str] = Field(default_factory=list)
    quality: list[str] = Field(default_factory=list)
    masking: list[str] = Field(default_factory=list)
    classification: list[str] = Field(default_factory=list)
    ner: bool = False
    ner_weights: bool = False


class PackManifest(_Strict):
    apiVersion: str
    kind: str
    metadata: PackMetadata
    requires: PackRequires = Field(default_factory=PackRequires)
    contents: PackContents = Field(default_factory=PackContents)
    mode: Literal["overlay", "replace"] = "overlay"
    checksum: Optional[str] = None
    signature: Optional[Any] = None

    @field_validator("apiVersion")
    @classmethod
    def _api(cls, v: str) -> str:
        if v != "redibis.io/pack/v1":
            raise ValueError(f"unsupported apiVersion: {v!r}")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v != "RedibisPack":
            raise ValueError(f"unsupported kind: {v!r}")
        return v

    @property
    def identity(self) -> str:
        return f"{self.metadata.id}@{self.metadata.version}"


@dataclass(frozen=True)
class PackFile:
    """One text file inside a pack."""

    relpath: str
    data: bytes
    sha256: str


@dataclass
class LoadedPack:
    """Fully loaded Redibis Pack (data-only; not yet applied to config)."""

    manifest: PackManifest
    files: dict[str, PackFile] = field(default_factory=dict)
    pack_sha256: str = ""
    source_kind: str = ""  # folder | zip | tar.gz
    source_name: str = ""
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False
    signature_status: Optional[dict[str, Any]] = None

    def text(self, relpath: str) -> Optional[str]:
        item = self.files.get(relpath)
        if item is None:
            return None
        return item.data.decode("utf-8")

    def yaml(self, relpath: str) -> Any:
        import yaml

        raw = self.text(relpath)
        if raw is None:
            return None
        return yaml.safe_load(raw)
