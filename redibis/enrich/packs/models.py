"""Typed models for enrichment pack manifest and runtime objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PackMetadata(_Strict):
    publisherId: str
    name: str
    version: str
    displayName: str = ""
    description: str = ""
    vendor: str = ""
    license: str = "LicenseRef-Proprietary"
    licenseUrl: Optional[str] = None
    homepage: Optional[str] = None
    support: Optional[str] = None

    @field_validator("publisherId")
    @classmethod
    def _publisher_id(cls, v: str) -> str:
        value = (v or "").strip()
        if not value or "/" in value or " " in value:
            raise ValueError("publisherId must be a non-empty reverse-DNS-style id")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        value = (v or "").strip()
        if not value or "/" in value or " " in value:
            raise ValueError("name must be a non-empty identifier without spaces or /")
        return value


class PackAuthenticity(_Strict):
    algorithm: str = "ed25519"
    keyId: str
    signatureFile: str
    signedPayload: str = "canonical-pack-digest-v1"

    @field_validator("signedPayload")
    @classmethod
    def _payload(cls, v: str) -> str:
        if v != "canonical-pack-digest-v1":
            raise ValueError("signedPayload must be canonical-pack-digest-v1")
        return v

    @field_validator("algorithm")
    @classmethod
    def _algo(cls, v: str) -> str:
        if v != "ed25519":
            raise ValueError("only ed25519 is reserved in enrichment pack v1")
        return v


class PackCompatibility(_Strict):
    redibis: str
    outputContract: str = "redibis.enrichment-delta/v1"


class PromptAssets(_Strict):
    domain: Optional[str] = None
    terminology: Optional[str] = None
    edgeCases: Optional[str] = None
    normal: list[str] = Field(default_factory=list)
    shared: list[str] = Field(default_factory=list)
    stages: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("stages")
    @classmethod
    def _stages(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        from redibis.enrich.workflow import LLM_STAGE_KINDS

        unknown = sorted(set(v or {}) - set(LLM_STAGE_KINDS))
        if unknown:
            raise ValueError(
                f"unknown prompt.stages keys {unknown}; allowed: {sorted(LLM_STAGE_KINDS)}"
            )
        return v


class ContextAssets(_Strict):
    company: Optional[str] = None
    dataDomains: Optional[str] = None
    columnGlossary: Optional[str] = None
    profilingGuidance: Optional[str] = None
    classification: Optional[str] = None


class SelectionPolicy(_Strict):
    glossaryTopK: int = Field(default=20, ge=0)
    examplesTopK: int = Field(default=3, ge=0)
    maxContextCharacters: int = Field(default=50_000, ge=1)


class GoldenExampleRef(_Strict):
    id: str
    input: str
    expectedDelta: str


class EvalsRef(_Strict):
    cases: str


class EnrichmentPackManifest(_Strict):
    apiVersion: str
    kind: str
    metadata: PackMetadata
    compatibility: PackCompatibility
    authenticity: Optional[PackAuthenticity] = None
    prompt: PromptAssets = Field(default_factory=PromptAssets)
    context: ContextAssets = Field(default_factory=ContextAssets)
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)
    examples: list[GoldenExampleRef] = Field(default_factory=list)
    evals: Optional[EvalsRef] = None

    @field_validator("apiVersion")
    @classmethod
    def _api(cls, v: str) -> str:
        if v != "redibis.enrichment/v1":
            raise ValueError(f"unsupported apiVersion: {v!r}")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v != "EnrichmentPack":
            raise ValueError(f"unsupported kind: {v!r}")
        return v

    @property
    def identity(self) -> str:
        return f"{self.metadata.publisherId}/{self.metadata.name}"

    @property
    def release_identity(self) -> str:
        return f"{self.identity}@{self.metadata.version}"


class GlossaryEntry(_Strict):
    canonicalName: str
    aliases: list[str] = Field(default_factory=list)
    businessName: str = ""
    definition: str
    domain: str = ""
    expectedLogicalTypes: list[str] = Field(default_factory=list)
    classifications: list[str] = Field(default_factory=list)
    profilingHints: dict[str, Any] = Field(default_factory=dict)
    relatedColumns: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("definition")
    @classmethod
    def _definition(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("definition must be non-empty")
        return v.strip()


class GlossaryDocument(_Strict):
    columns: list[GlossaryEntry] = Field(default_factory=list)


@dataclass(frozen=True)
class PackTextAsset:
    relpath: str
    text: str
    sha256: str


@dataclass(frozen=True)
class PackBinaryAsset:
    relpath: str
    data: bytes
    sha256: str


@dataclass
class LoadedEnrichmentPack:
    """Fully loaded, validated enrichment pack ready for context compilation."""

    manifest: EnrichmentPackManifest
    source_kind: str  # "folder" | "zip"
    source_name: str
    sha256: str
    texts: dict[str, PackTextAsset] = field(default_factory=dict)
    binaries: dict[str, PackBinaryAsset] = field(default_factory=dict)
    glossary: list[GlossaryEntry] = field(default_factory=list)
    golden_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    golden_deltas: dict[str, dict[str, Any]] = field(default_factory=dict)
    eval_cases: list[dict[str, Any]] = field(default_factory=list)
    signature_status: str = "unsigned"  # unsigned | unsupported | present
    warnings: list[str] = field(default_factory=list)

    def text(self, relpath: Optional[str]) -> Optional[str]:
        if not relpath:
            return None
        asset = self.texts.get(relpath)
        return asset.text if asset else None

    def provenance(self) -> dict[str, Any]:
        return {
            "api_version": self.manifest.apiVersion,
            "publisher_id": self.manifest.metadata.publisherId,
            "name": self.manifest.metadata.name,
            "version": self.manifest.metadata.version,
            "identity": self.manifest.release_identity,
            "sha256": self.sha256,
            "source_kind": self.source_kind,
            "source_name": self.source_name,
            "signature_status": self.signature_status,
            "license": self.manifest.metadata.license,
            "vendor": self.manifest.metadata.vendor,
            "display_name": self.manifest.metadata.displayName,
        }


@dataclass
class ContextReductionOperation:
    index: int
    operation: str
    asset_id: str
    asset_sha256: str
    reason_code: str
    characters_removed: int = 0
    rank: Optional[int] = None
    boundary: Optional[str] = None
    retained_characters: Optional[int] = None
    original_characters: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index,
            "operation": self.operation,
            "asset_id": self.asset_id,
            "asset_sha256": self.asset_sha256,
            "reason_code": self.reason_code,
            "characters_removed": self.characters_removed,
        }
        if self.rank is not None:
            out["rank"] = self.rank
        if self.boundary is not None:
            out["boundary"] = self.boundary
        if self.retained_characters is not None:
            out["retained_characters"] = self.retained_characters
        if self.original_characters is not None:
            out["original_characters"] = self.original_characters
        return out


@dataclass
class ContextReductionPlan:
    reduction_plan_id: str
    payload: dict[str, Any]
    operations: list[ContextReductionOperation]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reduction_plan_id": self.reduction_plan_id,
            "payload": self.payload,
            "operations": [op.to_dict() for op in self.operations],
            "summary": self.summary,
        }


@dataclass
class ContextReductionApproval:
    reduction_plan_id: str
    approved: bool = True
    approval_source: str = "interactive_cli"
    approved_by: str = ""
    approved_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reduction_plan_id": self.reduction_plan_id,
            "approved": self.approved,
            "approval_source": self.approval_source,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


@dataclass
class CompiledPackContext:
    """Rendered pack prompt sections plus selection provenance."""

    system_addendum: str
    user_addendum: str
    selected_glossary: list[str]
    omitted_glossary: list[str]
    selected_examples: list[str]
    omitted_examples: list[str]
    section_characters: dict[str, int]
    profile_prompt: str = ""
    reduction_plan: Optional[ContextReductionPlan] = None
    reduction_applied: bool = False
    approval: Optional[ContextReductionApproval] = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_provenance(self) -> dict[str, Any]:
        out = dict(self.provenance)
        out.update(
            {
                "selected_glossary_entries": list(self.selected_glossary),
                "omitted_glossary_entries": list(self.omitted_glossary),
                "selected_examples": list(self.selected_examples),
                "omitted_examples": list(self.omitted_examples),
                "section_characters": dict(self.section_characters),
                "context_reduction": {
                    "required": self.reduction_plan is not None,
                    "reduction_plan_id": (
                        self.reduction_plan.reduction_plan_id if self.reduction_plan else None
                    ),
                    "approved": bool(self.approval and self.approval.approved),
                    "approval_source": self.approval.approval_source if self.approval else None,
                    "applied": self.reduction_applied,
                    "operations": (
                        [op.to_dict() for op in self.reduction_plan.operations]
                        if self.reduction_plan and self.reduction_applied
                        else []
                    ),
                },
            }
        )
        return out
