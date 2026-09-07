"""Build and write canonical ``.rdbpack`` archives."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Union

from redibis.pack.archive import write_zip_file
from redibis.pack.canonical import (
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    README_NAME,
    build_checksums_document,
    canonicalize_file_bytes,
    dump_canonical_json,
    dump_canonical_yaml,
    format_checksum_field,
    canonical_pack_digest,
    parse_yaml_bytes,
)
from redibis.pack.config_allowlist import validate_pack_config
from redibis.pack.errors import PackValidationError
from redibis.pack.layout import validate_layout
from redibis.pack.models import PackManifest
from redibis.pack.requirements import validate_manifest_requires

PathLike = Union[str, Path]
FileMap = dict[str, bytes]


def _as_bytes(relpath: str, value: Any) -> bytes:
    if isinstance(value, bytes):
        return canonicalize_file_bytes(relpath, value)
    if isinstance(value, str):
        return canonicalize_file_bytes(relpath, value.encode("utf-8"))
    if relpath.lower().endswith((".yaml", ".yml")):
        return dump_canonical_yaml(value)
    if relpath.lower().endswith(".json"):
        return dump_canonical_json(value) + b"\n"
    raise PackValidationError(
        f"unsupported content type for {relpath}",
        errors=[f"unsupported content type for {relpath}"],
    )


def build_pack_files(
    manifest: PackManifest | Mapping[str, Any],
    sections: Mapping[str, Any] | None = None,
    *,
    readme: str = "Redibis Pack\n",
    check_registries: bool = True,
) -> tuple[FileMap, str]:
    """Assemble canonical file map + pack SHA (does not write to disk).

    ``sections`` maps relative paths (e.g. ``config/redibis.yaml``) to
    mappings, strings, or raw bytes.
    """
    if isinstance(manifest, PackManifest):
        manifest_obj = manifest
        manifest_payload = manifest.model_dump(mode="json", exclude_none=False)
    else:
        manifest_payload = dict(manifest)
        manifest_obj = PackManifest.model_validate(manifest_payload)

    validate_manifest_requires(manifest_obj, check_registries=check_registries)

    files: FileMap = {}
    for relpath, value in (sections or {}).items():
        rel = relpath.replace("\\", "/").lstrip("/")
        files[rel] = _as_bytes(rel, value)

    if "config/redibis.yaml" in files:
        validate_pack_config(parse_yaml_bytes(files["config/redibis.yaml"]))

    # Refuse local / raw_trained training corpora or model stamps (ArtifactResidencyGate).
    from redibis.training.residency import ArtifactResidencyGate

    ArtifactResidencyGate.check_pack_files(files, label="pack-build")

    # Stamp manifest without checksum first, then digest, then stamp.
    manifest_payload = manifest_obj.model_dump(mode="json", exclude_none=False)
    md = manifest_payload.get("metadata")
    if isinstance(md, dict):
        # Omit unset identity fields so pre-UUID golden packs keep a stable SHA.
        for key in ("uuid", "family_id", "parent_uuid"):
            if not md.get(key):
                md.pop(key, None)
    manifest_payload["checksum"] = None
    files[MANIFEST_NAME] = dump_canonical_yaml(manifest_payload)
    files[README_NAME] = _as_bytes(README_NAME, readme)

    validate_layout(manifest_obj, set(files) | {CHECKSUMS_NAME})

    pack_sha = canonical_pack_digest(files)
    manifest_payload["checksum"] = format_checksum_field(pack_sha)
    files[MANIFEST_NAME] = dump_canonical_yaml(manifest_payload)

    checksums = build_checksums_document(files, pack_sha256=pack_sha)
    files[CHECKSUMS_NAME] = dump_canonical_json(checksums) + b"\n"
    return files, pack_sha


def write_pack(
    path: PathLike,
    manifest: PackManifest | Mapping[str, Any],
    sections: Mapping[str, Any] | None = None,
    *,
    readme: str = "Redibis Pack\n",
    check_registries: bool = True,
) -> str:
    """Write a ``.rdbpack`` ZIP and return the canonical pack SHA-256 hex."""
    files, pack_sha = build_pack_files(
        manifest,
        sections,
        readme=readme,
        check_registries=check_registries,
    )
    write_zip_file(Path(path), files)
    return pack_sha
