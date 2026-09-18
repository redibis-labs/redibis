"""Construct a StorageBackend + bucket for a WorkspaceRef."""

from __future__ import annotations

import os
from typing import Optional

from redibis.store.storage_backend import LocalBackend, S3Backend, S3Config, StorageBackend
from redibis.workspace.model import DEFAULT_SLUG, WorkspaceRef
from redibis.workspace.prefix import PrefixedBackend


LOCAL_BUCKET = ""  # LocalBackend(root) / "" / key → root/key


def backend_for(
    ref: WorkspaceRef,
    *,
    backend: StorageBackend | None = None,
) -> tuple[StorageBackend, str]:
    """Return ``(backend, bucket)`` scoped to this workspace.

    Default workspace is not constructed here — ``stores_for('default')``
    returns the global singletons.
    """
    if ref.slug == DEFAULT_SLUG:
        raise ValueError("use stores_for('default') for the global store")
    if ref.kind == "local":
        return LocalBackend(ref.root), LOCAL_BUCKET
    bucket, prefix = ref.s3_parts()
    if backend is None:
        cfg = S3Config.from_env()
        if ref.endpoint:
            cfg.endpoint_url = ref.endpoint
        elif os.getenv("S3_ENDPOINT_URL"):
            cfg.endpoint_url = os.getenv("S3_ENDPOINT_URL")
        backend = S3Backend(cfg)
    if prefix:
        backend = PrefixedBackend(backend, prefix)
    return backend, bucket
