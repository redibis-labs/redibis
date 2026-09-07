"""Immutable published Redibis Pack store (``_meta/packs/``).

Layout (mirrors catalog ledger / PII decision overlays)::

    {bucket}/_meta/packs/
      index.json                    # uuid → PackRef + archive_sha256
      by-family/{family_id}.json    # ordered version history
      blobs/{uuid}.zip              # write-once archive

A UUID identifies an immutable pack *version*. ``publish`` is idempotent on
identical bytes and rejects ``family_id``+``version`` collisions with different
content. ``get`` verifies the stored blob hash and raises on mismatch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from redibis.pack.archive import detect_archive_kind, write_zip_bytes
from redibis.pack.canonical import sha256_bytes
from redibis.pack.identity import (
    CONTENTS_SUMMARY_KEYS,
    count_pack_contents,
    infer_pack_kind,
    load_trust_store,
    mint_pack_uuid,
)
from redibis.pack.loader import load_pack, pack_files_from_loaded
from redibis.store.storage_backend import StorageBackend

PathLike = Union[str, Path]


class PackStoreError(Exception):
    """Pack store read/write failure (tamper, missing, write-once)."""


class PackVersionCollisionError(PackStoreError):
    """Same family+version published with different bytes."""


@dataclass(frozen=True)
class PackRef:
    """Published pack version recorded in ``index.json``."""

    uuid: str
    family_id: str
    kind: str
    id: str
    version: str
    sha256: str
    size_bytes: int
    published_at: str
    author: str
    parent_uuid: Optional[str] = None
    signature: Optional[dict[str, Any]] = None
    contents: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.signature is None:
            payload["signature"] = None
        contents = {k: int(self.contents.get(k, 0)) for k in CONTENTS_SUMMARY_KEYS}
        for extra, value in self.contents.items():
            if extra not in contents:
                contents[str(extra)] = int(value)
        payload["contents"] = contents
        return payload

    @classmethod
    def from_dict(cls, data: MappingLike) -> "PackRef":
        raw = dict(data or {})
        contents = raw.get("contents") or {}
        if not isinstance(contents, dict):
            contents = {}
        sig = raw.get("signature")
        if sig is not None and not isinstance(sig, dict):
            sig = None
        return cls(
            uuid=str(raw.get("uuid") or ""),
            family_id=str(raw.get("family_id") or ""),
            kind=str(raw.get("kind") or "pack"),
            id=str(raw.get("id") or ""),
            version=str(raw.get("version") or ""),
            sha256=str(raw.get("sha256") or ""),
            size_bytes=int(raw.get("size_bytes") or 0),
            published_at=str(raw.get("published_at") or ""),
            author=str(raw.get("author") or ""),
            parent_uuid=(str(raw["parent_uuid"]) if raw.get("parent_uuid") else None),
            signature=sig,
            contents={str(k): int(v) for k, v in contents.items()},
        )


# ``from_dict`` annotation helper — avoid importing Mapping at runtime cycle risk.
MappingLike = Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_family_key(family_id: str) -> str:
    text = (family_id or "").strip()
    if not text or "/" in text or "\\" in text:
        raise PackStoreError(f"invalid pack family_id: {family_id!r}")
    return text


class PackStore:
    """Write-once pack blob store + uuid/family index under ``_meta/packs/``."""

    PREFIX = "_meta/packs"

    def __init__(
        self,
        backend: StorageBackend,
        bucket: str,
        *,
        require_signature: bool = False,
        trust_store: Optional[dict[str, str]] = None,
        trust_store_path: Optional[PathLike] = None,
    ):
        self.backend = backend
        self.bucket = bucket
        self.require_signature = bool(require_signature)
        keys = dict(trust_store or {})
        if trust_store_path:
            keys.update(load_trust_store(trust_store_path))
        self.trust_store = keys

    @classmethod
    def from_config(
        cls,
        backend: StorageBackend,
        bucket: str,
        config: Any = None,
    ) -> "PackStore":
        require = False
        trust_path = ""
        pack_cfg = getattr(config, "pack", None) if config is not None else None
        if pack_cfg is not None:
            require = bool(getattr(pack_cfg, "require_signature", False))
            trust_path = str(getattr(pack_cfg, "trust_store", "") or "")
        return cls(
            backend,
            bucket,
            require_signature=require,
            trust_store_path=trust_path or None,
        )

    def _index_key(self) -> str:
        return f"{self.PREFIX}/index.json"

    def _family_key(self, family_id: str) -> str:
        return f"{self.PREFIX}/by-family/{_safe_family_key(family_id)}.json"

    def _blob_key(self, uuid: str) -> str:
        uid = (uuid or "").strip()
        if not uid or "/" in uid or "\\" in uid:
            raise PackStoreError(f"invalid pack uuid: {uuid!r}")
        return f"{self.PREFIX}/blobs/{uid}.zip"

    def _load_index(self) -> dict[str, Any]:
        key = self._index_key()
        if not self.backend.exists(self.bucket, key):
            return {"packs": {}, "by_sha256": {}}
        raw = self.backend.get_json(self.bucket, key) or {}
        if not isinstance(raw, dict):
            return {"packs": {}, "by_sha256": {}}
        packs = raw.get("packs") if isinstance(raw.get("packs"), dict) else {}
        by_sha = raw.get("by_sha256") if isinstance(raw.get("by_sha256"), dict) else {}
        return {"packs": dict(packs), "by_sha256": dict(by_sha)}

    def _save_index(self, index: dict[str, Any]) -> None:
        self.backend.put_json(
            self.bucket,
            self._index_key(),
            {
                "packs": index.get("packs") or {},
                "by_sha256": index.get("by_sha256") or {},
            },
        )

    def _load_family(self, family_id: str) -> list[dict[str, Any]]:
        key = self._family_key(family_id)
        if not self.backend.exists(self.bucket, key):
            return []
        raw = self.backend.get_json(self.bucket, key) or {}
        versions = raw.get("versions") if isinstance(raw, dict) else None
        if not isinstance(versions, list):
            return []
        return [v for v in versions if isinstance(v, dict)]

    def _save_family(self, family_id: str, versions: list[dict[str, Any]]) -> None:
        self.backend.put_json(
            self.bucket,
            self._family_key(family_id),
            {"family_id": family_id, "versions": versions},
        )

    def _write_blob_once(self, uuid: str, data: bytes) -> None:
        key = self._blob_key(uuid)
        if self.backend.exists(self.bucket, key):
            raise PackStoreError(
                f"pack blob is immutable: blobs/{uuid}.zip already exists"
            )
        self.backend.put_bytes(
            self.bucket,
            key,
            data,
            content_type="application/zip",
        )

    def _ref_from_index(self, uuid: str, index: dict[str, Any] | None = None) -> Optional[PackRef]:
        index = index or self._load_index()
        raw = (index.get("packs") or {}).get(uuid)
        if not isinstance(raw, dict):
            return None
        return PackRef.from_dict(raw)

    def head(self, uuid: str) -> Optional[PackRef]:
        uid = (uuid or "").strip()
        if not uid:
            return None
        return self._ref_from_index(uid)

    def list(self, *, family_id: str | None = None) -> list[PackRef]:
        index = self._load_index()
        refs = [
            PackRef.from_dict(raw)
            for raw in (index.get("packs") or {}).values()
            if isinstance(raw, dict)
        ]
        if family_id:
            wanted = family_id.strip()
            refs = [r for r in refs if r.family_id == wanted]
        refs.sort(key=lambda r: (r.family_id, r.published_at, r.version, r.uuid))
        return refs

    def resolve_stack(self, uuids: list[str]) -> list[PackRef]:
        missing: list[str] = []
        out: list[PackRef] = []
        for uid in uuids:
            ref = self.head(str(uid))
            if ref is None:
                missing.append(str(uid))
            else:
                out.append(ref)
        if missing:
            raise PackStoreError(
                f"pack uuid(s) not in store: {', '.join(missing)}"
            )
        return out

    def get(self, uuid: str) -> bytes:
        """Return blob bytes after verifying archive ``sha256``. Raises on mismatch."""
        uid = (uuid or "").strip()
        index = self._load_index()
        raw = (index.get("packs") or {}).get(uid)
        if not isinstance(raw, dict):
            raise PackStoreError(f"unknown pack uuid: {uuid}")
        ref = PackRef.from_dict(raw)
        expected_archive = str(raw.get("archive_sha256") or ref.sha256 or "")
        key = self._blob_key(ref.uuid)
        try:
            data = self.backend.get_bytes(self.bucket, key)
        except KeyError as exc:
            raise PackStoreError(f"pack blob missing for uuid: {uuid}") from exc
        actual = sha256_bytes(data)
        if actual != expected_archive:
            raise PackStoreError(
                f"pack sha256 mismatch for {ref.uuid}: "
                f"expected {expected_archive} actual {actual}"
            )
        return data

    def publish(self, archive: PathLike, *, author: str) -> PackRef:
        """Publish an archive. Identical bytes return the existing ``PackRef``."""
        path = Path(archive).expanduser()
        if not path.exists():
            raise PackStoreError(f"pack archive not found: {path}")

        kind = detect_archive_kind(path)
        if kind == "zip":
            data = path.read_bytes()
        else:
            loaded_for_zip = load_pack(
                path,
                require_signature=self.require_signature,
                trust_store=self.trust_store or None,
            )
            data = write_zip_bytes(pack_files_from_loaded(loaded_for_zip))

        archive_sha = sha256_bytes(data)
        index = self._load_index()
        existing_uuid = (index.get("by_sha256") or {}).get(archive_sha)
        if existing_uuid:
            ref = self._ref_from_index(str(existing_uuid), index)
            if ref is not None:
                return ref

        pack = load_pack(
            path,
            require_signature=self.require_signature,
            trust_store=self.trust_store or None,
        )
        md = pack.manifest.metadata
        family_id = (md.family_id or "").strip()
        if not family_id:
            raise PackStoreError("pack family_id is required after identity back-fill")
        uuid_value = (md.uuid or "").strip() or mint_pack_uuid()
        kind = infer_pack_kind(pack.manifest.contents, pack_id=md.id)
        contents = count_pack_contents(pack_files_from_loaded(pack), pack.manifest)
        signature = pack.signature_status
        if signature is None and isinstance(pack.manifest.signature, dict):
            signature = dict(pack.manifest.signature)

        incoming = PackRef(
            uuid=uuid_value,
            family_id=family_id,
            kind=kind,
            id=md.id,
            version=md.version,
            sha256=pack.pack_sha256,
            size_bytes=len(data),
            published_at=_utc_now_iso(),
            author=str(author or md.author or ""),
            parent_uuid=md.parent_uuid,
            signature=signature,
            contents=contents,
        )

        collision = None
        for raw in (index.get("packs") or {}).values():
            if not isinstance(raw, dict):
                continue
            other = PackRef.from_dict(raw)
            if other.family_id == incoming.family_id and other.version == incoming.version:
                other_archive = str(raw.get("archive_sha256") or other.sha256 or "")
                if other_archive != archive_sha or other.sha256 != incoming.sha256:
                    collision = other
                break
        if collision is not None:
            raise PackVersionCollisionError(
                f"pack version collision for {incoming.family_id}@{incoming.version}: "
                f"existing uuid={collision.uuid} sha256={collision.sha256} "
                f"incoming uuid={incoming.uuid} sha256={incoming.sha256}"
            )

        already = self._ref_from_index(incoming.uuid, index)
        if already is not None:
            if already.sha256 != incoming.sha256:
                raise PackStoreError(
                    f"pack uuid {incoming.uuid} already published with "
                    f"sha256={already.sha256}; incoming sha256={incoming.sha256}"
                )
            return already

        self._write_blob_once(incoming.uuid, data)

        packs = dict(index.get("packs") or {})
        by_sha = dict(index.get("by_sha256") or {})
        payload = incoming.to_dict()
        payload["archive_sha256"] = archive_sha
        packs[incoming.uuid] = payload
        by_sha[archive_sha] = incoming.uuid
        self._save_index({"packs": packs, "by_sha256": by_sha})

        history = self._load_family(incoming.family_id)
        if not any(h.get("uuid") == incoming.uuid for h in history):
            history.append(
                {
                    "uuid": incoming.uuid,
                    "version": incoming.version,
                    "sha256": incoming.sha256,
                    "parent_uuid": incoming.parent_uuid,
                    "published_at": incoming.published_at,
                    "author": incoming.author,
                }
            )
            history.sort(key=lambda h: (str(h.get("published_at") or ""), str(h.get("version") or "")))
            self._save_family(incoming.family_id, history)

        return incoming

    def history(self, family_id: str) -> list[PackRef]:
        """Version chain for a family (``parent_uuid`` order when present)."""
        rows = self._load_family(family_id)
        by_uuid = {r.uuid: r for r in self.list(family_id=family_id)}
        if not by_uuid:
            return []
        # Prefer explicit parent chain when every node is present; else published_at.
        children: dict[Optional[str], list[PackRef]] = {}
        for row in rows:
            uid = str(row.get("uuid") or "")
            ref = by_uuid.get(uid)
            if ref is None:
                continue
            children.setdefault(ref.parent_uuid, []).append(ref)
        roots = children.get(None) or []
        if not roots:
            return sorted(by_uuid.values(), key=lambda r: (r.published_at, r.version, r.uuid))
        ordered: list[PackRef] = []
        seen: set[str] = set()

        def _walk(node: PackRef) -> None:
            if node.uuid in seen:
                return
            seen.add(node.uuid)
            ordered.append(node)
            for child in sorted(
                children.get(node.uuid) or [],
                key=lambda r: (r.published_at, r.version, r.uuid),
            ):
                _walk(child)

        for root in sorted(roots, key=lambda r: (r.published_at, r.version, r.uuid)):
            _walk(root)
        for ref in sorted(by_uuid.values(), key=lambda r: (r.published_at, r.version, r.uuid)):
            if ref.uuid not in seen:
                ordered.append(ref)
        return ordered
