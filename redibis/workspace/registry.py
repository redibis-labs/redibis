"""Known workspaces for this server. Stored at ``<REDIBIS_CONFIGS_DIR>/workspaces.json``.

``remove`` forgets a slug; it never deletes workspace data.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from redibis.workspace.atomic import atomic_put_json
from redibis.workspace.model import (
    DEFAULT_SLUG,
    WORKSPACE_LAYOUT,
    WorkspaceDenied,
    WorkspaceError,
    WorkspaceNotFound,
    WorkspaceRef,
    _utc_now_iso,
    parse_s3_url,
    s3_root,
    slugify,
)
from redibis.workspace.paths import (
    configs_dir,
    default_storage_root,
    resolve_local_folder,
)
from redibis.workspace.prefix import PrefixedBackend
from redibis.store.storage_backend import S3Backend, S3Config, StorageBackend


def _workspaces_config() -> Any:
    from redibis.config import RedibisConfig

    path = os.environ.get("REDIBIS_CONFIG")
    if path:
        try:
            return RedibisConfig.from_yaml(path).workspaces
        except Exception:
            pass
    return RedibisConfig().workspaces


class WorkspaceRegistry:
    """Process-local registry backed by a JSON file in the configs dir."""

    def __init__(self, path: Path | None = None):
        self._path = path or (configs_dir() / "workspaces.json")
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[WorkspaceRef]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = raw.get("workspaces") if isinstance(raw, dict) else raw
        out: list[WorkspaceRef] = []
        for row in items or []:
            if isinstance(row, dict):
                ref = WorkspaceRef.from_dict(row)
                if ref.slug and ref.slug != DEFAULT_SLUG:
                    out.append(ref)
        return out

    def _save(self, refs: list[WorkspaceRef]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workspaces": [r.to_dict() for r in refs]}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def default(self) -> WorkspaceRef:
        """The global active store, always present, never stored in the file."""
        if os.getenv("USE_LOCAL_STORAGE", "false").lower() == "true" or not os.getenv("S3_ENDPOINT_URL"):
            kind = "local"
            root = str(default_storage_root())
        else:
            kind = "s3"
            bucket = os.getenv("S3_CONTRACTS_BUCKET", "active-contracts")
            root = f"s3://{bucket}"
        return WorkspaceRef(
            slug=DEFAULT_SLUG,
            name="default",
            kind=kind,
            root=root,
            created="",
            notes="global active store",
        )

    def list(self) -> list[WorkspaceRef]:
        with self._lock:
            return [self.default(), *self._load()]

    def get(self, slug: str) -> WorkspaceRef:
        slug = (slug or DEFAULT_SLUG).strip() or DEFAULT_SLUG
        if slug == DEFAULT_SLUG:
            return self.default()
        with self._lock:
            for ref in self._load():
                if ref.slug == slug:
                    return ref
        raise WorkspaceNotFound(f"unknown workspace {slug!r}")

    def _unique_slug(self, base: str, existing: list[WorkspaceRef]) -> str:
        slug = slugify(base)
        if slug == DEFAULT_SLUG:
            slug = "ws"
        taken = {r.slug for r in existing} | {DEFAULT_SLUG}
        if slug not in taken:
            return slug
        n = 2
        while f"{slug}-{n}" in taken:
            n += 1
        return f"{slug}-{n}"

    def add_local(
        self,
        name: str,
        folder: str,
        *,
        read_only: bool = False,
        notes: str = "",
        create: bool = True,
        cfg: Any = None,
    ) -> WorkspaceRef:
        cfg = cfg if cfg is not None else _workspaces_config()
        allowed = list(getattr(cfg, "allowed_roots", None) or [])
        resolved = resolve_local_folder(
            folder, allowed, must_exist=not create,
        )
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        with self._lock:
            refs = self._load()
            for ref in refs:
                if ref.kind == "local" and Path(ref.root).resolve() == resolved:
                    return ref
            slug = self._unique_slug(name or resolved.name, refs)
            ref = WorkspaceRef(
                slug=slug,
                name=name or resolved.name,
                kind="local",
                root=str(resolved),
                created=_utc_now_iso(),
                read_only=read_only,
                notes=notes,
            )
            refs.append(ref)
            self._save(refs)
        _ensure_manifest(ref)
        return ref

    def add_s3(
        self,
        name: str,
        bucket: str,
        prefix: str = "",
        *,
        endpoint: str | None = None,
        read_only: bool = False,
        notes: str = "",
        backend: StorageBackend | None = None,
    ) -> WorkspaceRef:
        bucket = (bucket or "").strip()
        if not bucket:
            raise WorkspaceError("s3 bucket is required")
        prefix = (prefix or "").strip().strip("/")
        probe = backend
        if probe is None:
            cfg = S3Config.from_env()
            if endpoint:
                cfg.endpoint_url = endpoint
            probe = S3Backend(cfg)
        # Reachability: listing the prefix must succeed (empty is fine).
        try:
            wrapped = PrefixedBackend(probe, prefix) if prefix else probe
            wrapped.list_keys(bucket, prefix="")
        except Exception as exc:
            raise WorkspaceError(f"cannot list s3://{bucket}/{prefix}: {exc}") from exc
        root = s3_root(bucket, prefix)
        with self._lock:
            refs = self._load()
            for ref in refs:
                if ref.kind == "s3" and ref.root == root:
                    return ref
            slug = self._unique_slug(name or bucket, refs)
            ref = WorkspaceRef(
                slug=slug,
                name=name or bucket,
                kind="s3",
                root=root,
                created=_utc_now_iso(),
                read_only=read_only,
                notes=notes,
                endpoint=endpoint or "",
            )
            refs.append(ref)
            self._save(refs)
        _ensure_manifest(ref, backend=probe)
        return ref

    def add_from_root(self, root: str, *, name: str = "", read_only: bool = False) -> WorkspaceRef:
        text = (root or "").strip()
        if text.startswith("s3://"):
            bucket, prefix = parse_s3_url(text)
            return self.add_s3(name or bucket, bucket, prefix, read_only=read_only)
        return self.add_local(name or Path(text).name, text, read_only=read_only)

    def remove(self, slug: str) -> None:
        """Forget the slug. Never deletes objects on disk or in MinIO."""
        slug = (slug or "").strip()
        if not slug or slug == DEFAULT_SLUG:
            raise WorkspaceDenied("cannot remove the default workspace")
        with self._lock:
            refs = self._load()
            kept = [r for r in refs if r.slug != slug]
            if len(kept) == len(refs):
                raise WorkspaceNotFound(f"unknown workspace {slug!r}")
            self._save(kept)


_REGISTRY: WorkspaceRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> WorkspaceRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = WorkspaceRegistry()
        return _REGISTRY


def reset_registry() -> None:
    """Drop the process singleton (tests)."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def _ensure_manifest(ref: WorkspaceRef, *, backend: StorageBackend | None = None) -> None:
    from redibis.workspace.backends import backend_for

    be, bucket = backend_for(ref, backend=backend)
    key = WORKSPACE_LAYOUT["manifest"]
    if be.exists(bucket, key):
        return
    atomic_put_json(be, bucket, key, {
        "name": ref.name,
        "slug": ref.slug,
        "kind": ref.kind,
        "root": ref.root,
        "created": ref.created,
        "layout": WORKSPACE_LAYOUT,
    })
