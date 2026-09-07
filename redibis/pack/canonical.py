"""Canonical serialization and content-addressed digests for Redibis Packs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml

CHECKSUMS_NAME = "CHECKSUMS.json"
MANIFEST_NAME = "pack.yaml"
README_NAME = "README.md"

# Files excluded from the canonical pack digest (they *contain* the digest).
DIGEST_EXCLUDED = frozenset({CHECKSUMS_NAME})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump_canonical_json(payload: Any) -> bytes:
    """Stable JSON bytes for hashing and CHECKSUMS.json."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def dump_canonical_yaml(payload: Any) -> bytes:
    """Stable YAML bytes (sorted keys, unicode, trailing newline)."""
    text = yaml.safe_dump(
        payload,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
        width=120,
    )
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def parse_yaml_bytes(data: bytes) -> Any:
    return yaml.safe_load(data.decode("utf-8"))


def canonicalize_file_bytes(relpath: str, data: bytes) -> bytes:
    """Re-encode YAML/JSON payloads into a stable form; leave Markdown as UTF-8 text."""
    lower = relpath.lower()
    if lower.endswith((".yaml", ".yml")):
        parsed = parse_yaml_bytes(data)
        return dump_canonical_yaml(parsed)
    if lower.endswith(".json"):
        parsed = json.loads(data.decode("utf-8"))
        return dump_canonical_json(parsed) + b"\n"
    if lower.endswith(".md"):
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ValueError(f"binary content not allowed in {relpath}")
        if not text.endswith("\n"):
            text += "\n"
        return text.encode("utf-8")
    raise ValueError(f"unsupported pack file type: {relpath}")


def manifest_bytes_for_digest(manifest_data: bytes) -> bytes:
    """Canonical pack.yaml bytes with ``checksum`` nulled for digest input."""
    raw = parse_yaml_bytes(manifest_data)
    if not isinstance(raw, dict):
        raise ValueError("pack.yaml must be a mapping")
    payload = dict(raw)
    payload["checksum"] = None
    return dump_canonical_yaml(payload)


def canonical_pack_digest(files: dict[str, bytes]) -> str:
    """SHA-256 over sorted ``(path, bytes)`` excluding CHECKSUMS.json.

    ``pack.yaml`` contributes its checksum-nulled canonical form so stamping
    the digest into the manifest does not change the pack SHA.
    """
    h = hashlib.sha256()
    for relpath in sorted(files):
        if relpath in DIGEST_EXCLUDED:
            continue
        data = files[relpath]
        if relpath == MANIFEST_NAME:
            data = manifest_bytes_for_digest(data)
        enc_path = relpath.encode("utf-8")
        h.update(len(enc_path).to_bytes(4, "big"))
        h.update(enc_path)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def build_checksums_document(files: dict[str, bytes], *, pack_sha256: str) -> dict[str, Any]:
    """Per-file SHA-256 map plus the canonical pack digest."""
    file_hashes = {
        path: sha256_bytes(data)
        for path, data in sorted(files.items())
        if path != CHECKSUMS_NAME
    }
    return {
        "version": 1,
        "algorithm": "sha256",
        "files": file_hashes,
        "pack_sha256": pack_sha256,
    }


def format_checksum_field(pack_sha256: str) -> str:
    return f"sha256:{pack_sha256}"


def parse_checksum_field(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("sha256:"):
        return text[len("sha256:") :]
    return text
