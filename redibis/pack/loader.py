"""Load and verify portable Redibis Packs (folder / zip / tar.gz)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional, Union

from redibis.pack.archive import (
    MAX_FILE_BYTES,
    MAX_FILE_COUNT,
    MAX_UNCOMPRESSED_BYTES,
    detect_archive_kind,
    normalize_relpath,
    read_tar_files,
    read_zip_files,
    suffix_allowed,
)
from redibis.pack.canonical import (
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    canonical_pack_digest,
    parse_checksum_field,
    parse_yaml_bytes,
    sha256_bytes,
)
from redibis.pack.config_allowlist import validate_pack_config
from redibis.pack.errors import PackLoadError, PackValidationError
from redibis.pack.identity import ensure_pack_identity, load_trust_store, verify_pack_signature
from redibis.pack.layout import validate_layout
from redibis.pack.models import LoadedPack, PackFile, PackManifest
from redibis.pack.requirements import validate_manifest_requires

PathLike = Union[str, Path]


def _load_folder(root: Path) -> dict[str, bytes]:
    if root.is_symlink():
        raise PackLoadError("pack folder must not be a symlink")
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            raise PackLoadError(f"symlinks are not allowed: {path}")
        rel = normalize_relpath(str(path.relative_to(root)))
        if not suffix_allowed(rel):
            raise PackLoadError(f"unsupported pack file extension: {rel}")
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise PackLoadError(f"file exceeds size limit: {rel}")
        total += len(data)
        if len(files) + 1 > MAX_FILE_COUNT:
            raise PackLoadError("pack exceeds maximum file count")
        if total > MAX_UNCOMPRESSED_BYTES:
            raise PackLoadError("pack exceeds maximum uncompressed size")
        files[rel] = data
    return files


def _verify_checksums(files: dict[str, bytes]) -> str:
    if CHECKSUMS_NAME not in files:
        raise PackValidationError(
            f"missing {CHECKSUMS_NAME}",
            errors=[f"missing {CHECKSUMS_NAME}"],
        )
    try:
        doc = json.loads(files[CHECKSUMS_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackValidationError(
            f"invalid {CHECKSUMS_NAME}: {exc}",
            errors=[str(exc)],
        ) from exc
    if not isinstance(doc, dict):
        raise PackValidationError(f"{CHECKSUMS_NAME} must be a mapping")

    declared = doc.get("files") or {}
    if not isinstance(declared, dict):
        raise PackValidationError(f"{CHECKSUMS_NAME}.files must be a mapping")

    errors: list[str] = []
    for rel, data in files.items():
        if rel == CHECKSUMS_NAME:
            continue
        expected = declared.get(rel)
        if expected is None:
            errors.append(f"missing checksum entry for {rel}")
            continue
        actual = sha256_bytes(data)
        if actual != expected:
            errors.append(f"checksum mismatch for {rel}")
    for rel in declared:
        if rel not in files:
            errors.append(f"checksum entry for missing file: {rel}")
    if errors:
        raise PackValidationError("pack checksum verification failed", errors=errors)

    pack_sha = canonical_pack_digest(files)
    declared_pack = doc.get("pack_sha256")
    if declared_pack and declared_pack != pack_sha:
        raise PackValidationError(
            "canonical pack SHA mismatch",
            errors=[
                f"CHECKSUMS.json pack_sha256={declared_pack} computed={pack_sha}",
            ],
        )
    return pack_sha


def _assemble(
    files: dict[str, bytes],
    *,
    source_kind: str,
    source_name: str,
    require_signature: bool = False,
    trust_store: Mapping[str, str] | None = None,
) -> LoadedPack:
    if MANIFEST_NAME not in files:
        raise PackLoadError(f"{MANIFEST_NAME} not found")

    pack_sha = _verify_checksums(files)

    raw_manifest = parse_yaml_bytes(files[MANIFEST_NAME])
    if not isinstance(raw_manifest, dict):
        raise PackValidationError("pack.yaml must be a mapping")
    try:
        manifest = PackManifest.model_validate(raw_manifest)
    except Exception as exc:
        raise PackValidationError(
            f"manifest validation failed: {exc}",
            errors=[str(exc)],
        ) from exc

    stamped = parse_checksum_field(manifest.checksum)
    if stamped and stamped != pack_sha:
        raise PackValidationError(
            "pack.yaml checksum does not match pack digest",
            errors=[f"pack.yaml checksum={stamped} computed={pack_sha}"],
        )

    validate_layout(manifest, set(files))
    validate_manifest_requires(manifest, check_registries=True)

    if "config/redibis.yaml" in files:
        validate_pack_config(parse_yaml_bytes(files["config/redibis.yaml"]))

    from redibis.training.residency import ArtifactResidencyGate

    ArtifactResidencyGate.check_pack_files(files, label="pack-load")

    ensure_pack_identity(manifest, pack_sha)
    signature_status = verify_pack_signature(
        manifest,
        pack_sha256=pack_sha,
        trust_store=trust_store,
        require=require_signature,
    )

    pack_files = {
        rel: PackFile(relpath=rel, data=data, sha256=sha256_bytes(data))
        for rel, data in files.items()
    }
    return LoadedPack(
        manifest=manifest,
        files=pack_files,
        pack_sha256=pack_sha,
        source_kind=source_kind,
        source_name=source_name,
        signature_status=signature_status,
    )


def load_pack(
    path: PathLike,
    *,
    require_signature: bool = False,
    trust_store: Mapping[str, str] | None = None,
    trust_store_path: Optional[PathLike] = None,
) -> LoadedPack:
    """Load a Redibis Pack from a folder, ``.rdbpack`` ZIP, or tar.gz."""
    target = Path(path).expanduser()
    if not target.exists():
        raise PackLoadError(f"pack path not found: {target}")
    kind = detect_archive_kind(target)
    if kind == "folder":
        files = _load_folder(target)
    elif kind == "zip":
        files = read_zip_files(target, manifest_name=MANIFEST_NAME)
    else:
        files = read_tar_files(target)
    keys = dict(trust_store or {})
    if trust_store_path:
        keys.update(load_trust_store(trust_store_path))
    return _assemble(
        files,
        source_kind=kind,
        source_name=target.name,
        require_signature=require_signature,
        trust_store=keys or None,
    )


def pack_files_from_loaded(pack: LoadedPack) -> dict[str, bytes]:
    """Extract the file map from a loaded pack (for rewrite / round-trip)."""
    return {rel: item.data for rel, item in pack.files.items()}
