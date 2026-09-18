"""Workspace identity and on-disk / object-store layout.

ContractStore still writes ``active/{table}.yaml`` (code wins over the brief's
``contracts/`` folder). The index ``path`` field is that storage key.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


DEFAULT_SLUG = "default"

WORKSPACE_LAYOUT = {
    "manifest": "workspace.json",
    "index": "index.json",
    "contracts": "active/",  # ContractStore.ACTIVE_PREFIX — not contracts/
    "runs": "runs/",
    "meta": "_meta/",
    "batches": "batches/",
}


class WorkspaceError(ValueError):
    """Base error for workspace registry / path / backend failures."""


class WorkspaceNotFound(WorkspaceError):
    """Unknown workspace slug."""


class WorkspaceDenied(WorkspaceError):
    """Path containment or policy refusal."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in (value or "").strip().lower())
    collapsed = "-".join(part for part in out.split("-") if part)
    return collapsed.strip("-") or "ws"


@dataclass(frozen=True)
class WorkspaceRef:
    slug: str
    name: str
    kind: str  # "local" | "s3"
    root: str  # absolute folder, or "s3://bucket/prefix"
    created: str
    read_only: bool = False
    notes: str = ""
    endpoint: str = ""  # optional S3/MinIO endpoint override

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkspaceRef":
        return cls(
            slug=str(raw.get("slug") or ""),
            name=str(raw.get("name") or raw.get("slug") or ""),
            kind=str(raw.get("kind") or "local"),
            root=str(raw.get("root") or ""),
            created=str(raw.get("created") or _utc_now_iso()),
            read_only=bool(raw.get("read_only") or False),
            notes=str(raw.get("notes") or ""),
            endpoint=str(raw.get("endpoint") or ""),
        )

    def s3_parts(self) -> tuple[str, str]:
        """Return ``(bucket, key_prefix)`` for an s3 workspace."""
        if self.kind != "s3":
            raise WorkspaceError(f"workspace {self.slug!r} is not s3")
        parsed = urlparse(self.root)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise WorkspaceError(f"invalid s3 root {self.root!r}")
        prefix = (parsed.path or "").lstrip("/")
        return parsed.netloc, prefix.rstrip("/")


def parse_s3_url(url: str) -> tuple[str, str]:
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "s3" or not parsed.netloc:
        raise WorkspaceError(f"expected s3://bucket/prefix, got {url!r}")
    prefix = (parsed.path or "").lstrip("/").rstrip("/")
    return parsed.netloc, prefix


def s3_root(bucket: str, prefix: str) -> str:
    prefix = (prefix or "").strip("/")
    return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
