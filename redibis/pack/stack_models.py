"""Pack layer models and effective stack result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.config import RedibisConfig
from redibis.pii.regex_overrides import RegexOverrides


@dataclass(frozen=True)
class PackLayerRef:
    """One resolved pack layer (id / version / checksum) for audit."""

    id: str
    version: str
    checksum: str
    mode: str = "overlay"
    path: Optional[str] = None
    source: str = "applied"  # builtin | config | import | path
    uuid: str = ""
    family_id: str = ""
    parent_uuid: Optional[str] = None
    author: str = ""
    kind: str = ""
    size_bytes: int = 0
    published_at: str = ""
    signature: Optional[Any] = None
    contents_summary: dict[str, int] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def sha256(self) -> str:
        """Alias for ``checksum`` so stack UUID hashing can treat layers as PackRefs."""
        return self.checksum

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "checksum": self.checksum,
            "mode": self.mode,
            "path": self.path,
            "source": self.source,
            "identity": self.identity,
        }
        if self.uuid:
            out["uuid"] = self.uuid
        if self.family_id:
            out["family_id"] = self.family_id
        if self.parent_uuid:
            out["parent_uuid"] = self.parent_uuid
        if self.author:
            out["author"] = self.author
        if self.kind:
            out["kind"] = self.kind
        if self.size_bytes:
            out["size_bytes"] = self.size_bytes
        if self.published_at:
            out["published_at"] = self.published_at
        if self.signature is not None:
            out["signature"] = self.signature
        if self.contents_summary:
            out["contents"] = dict(self.contents_summary)
        return out


@dataclass
class AppliedPackStack:
    """Result of resolving one or more packs onto a base config."""

    config: RedibisConfig
    layers: list[PackLayerRef] = field(default_factory=list)
    regex_overrides: Optional[RegexOverrides] = None
    context_tokens: dict[str, tuple[str, ...]] = field(default_factory=dict)
    token_normalizers: tuple[str, ...] = ("casefold", "accent_fold", "arabic_fold")
    phone_locale: dict[str, Any] = field(default_factory=dict)
    ner_phrases: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Named documents collected from packs (Phase 5+ wiring).
    quality_rulesets: dict[str, Any] = field(default_factory=dict)
    masking_plans: dict[str, Any] = field(default_factory=dict)
    classification_packs: dict[str, Any] = field(default_factory=dict)
    prompt_templates: dict[str, str] = field(default_factory=dict)
    behavior_policy_paths: list[str] = field(default_factory=list)

    def layer_audit(self) -> list[dict[str, Any]]:
        return [layer.to_dict() for layer in self.layers]
