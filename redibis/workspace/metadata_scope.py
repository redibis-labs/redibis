"""Scope ContractMetadataStore keys when S3_METADATA_BUCKET is shared.

The default workspace keeps the historical unprefixed layout in
``S3_METADATA_BUCKET``. Non-default S3 workspaces get a deterministic
prefix derived from slug + contracts bucket + S3 key prefix, written
through the unwrapped inner backend so ``PrefixedBackend`` does not
double-prefix. Local workspaces stay isolated by filesystem root.
"""

from __future__ import annotations

import os
from typing import Any

from redibis.store.storage_backend import StorageBackend
from redibis.workspace.model import DEFAULT_SLUG, WorkspaceRef
from redibis.workspace.prefix import unwrap_local


def contract_store_metadata_kwargs(
    ref: WorkspaceRef,
    backend: StorageBackend,
    bucket: str,
) -> dict[str, Any]:
    """Extra ``ContractStore`` kwargs so dedicated metadata does not collide.

    Returns ``{}`` for the default workspace (legacy empty prefix) and for
    local workspaces (physical isolation via ``LocalBackend`` root).
    """
    meta_bucket = os.getenv("S3_METADATA_BUCKET")
    if not meta_bucket or ref.slug == DEFAULT_SLUG or ref.kind != "s3":
        return {}
    inner, _accumulated = unwrap_local(backend)
    _, ws_prefix = ref.s3_parts()
    parts = ["workspaces", ref.slug, bucket]
    if ws_prefix:
        parts.append(ws_prefix)
    return {
        "metadata_bucket": meta_bucket,
        "metadata_prefix": "/".join(p for p in parts if p),
        "metadata_backend": inner,
    }
