"""StorageBackend wrapper that scopes every key under a prefix.

Used for MinIO/S3 workspaces so existing stores need no key-path changes —
only construction. Local workspaces use ``LocalBackend(root)`` with an empty
bucket instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

from redibis.store.storage_backend import StorageBackend


class PrefixedBackend(StorageBackend):
    """Delegate that prepends ``prefix/`` to every key.

    ``list_keys`` returns keys *without* the prefix so ContractStore and friends
    keep seeing ``active/{table}.yaml``.
    """

    def __init__(self, inner: StorageBackend, prefix: str = ""):
        self._inner = inner
        self._prefix = (prefix or "").strip("/")

    def _key(self, key: str) -> str:
        key = (key or "").lstrip("/")
        if not self._prefix:
            return key
        if not key:
            return self._prefix
        return f"{self._prefix}/{key}"

    def _strip(self, key: str) -> str:
        if not self._prefix:
            return key
        pfx = self._prefix + "/"
        if key.startswith(pfx):
            return key[len(pfx):]
        if key == self._prefix:
            return ""
        return key

    def ping(self) -> bool:
        return bool(self._inner.ping())

    def put_bytes(self, bucket: str, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> str:
        return self._inner.put_bytes(bucket, self._key(key), data, content_type)

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self._inner.get_bytes(bucket, self._key(key))

    def exists(self, bucket: str, key: str) -> bool:
        return self._inner.exists(bucket, self._key(key))

    def list_keys(self, bucket: str, prefix: str = "", delimiter: str = "") -> list[str]:
        raw = self._inner.list_keys(bucket, prefix=self._key(prefix), delimiter=delimiter)
        return [self._strip(k) for k in raw if k == self._prefix or k.startswith(self._prefix + "/") or not self._prefix]

    def delete(self, bucket: str, key: str) -> None:
        self._inner.delete(bucket, self._key(key))

    def put_file(self, bucket: str, key: str, local_path: Union[str, Path],
                 content_type: str = "application/octet-stream") -> str:
        return self._inner.put_file(bucket, self._key(key), local_path, content_type=content_type)


def unwrap_local(backend: StorageBackend) -> tuple[Any, str]:
    """Return ``(innermost backend, accumulated prefix)``."""
    prefix_parts: list[str] = []
    current: Any = backend
    while isinstance(current, PrefixedBackend):
        if current._prefix:
            prefix_parts.append(current._prefix)
        current = current._inner
    return current, "/".join(prefix_parts)
