"""Atomic JSON writes for workspace manifests and indexes."""

from __future__ import annotations

import json
import os
from typing import Any

from redibis.store.storage_backend import LocalBackend, StorageBackend
from redibis.workspace.prefix import unwrap_local


def atomic_put_json(backend: StorageBackend, bucket: str, key: str, obj: Any) -> None:
    """Write JSON. On a local filesystem this is temp-file + replace."""
    inner, prefix = unwrap_local(backend)
    if isinstance(inner, LocalBackend):
        full_key = f"{prefix}/{key}" if prefix else key
        path = inner._full_path(bucket, full_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        return
    backend.put_json(bucket, key, obj)
