"""Secure enrichment pack loader for folders and ZIP archives."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from redibis.enrich.packs.errors import PackLoadError, PackValidationError
from redibis.enrich.packs.models import (
    EnrichmentPackManifest,
    GlossaryDocument,
    GlossaryEntry,
    LoadedEnrichmentPack,
    PackBinaryAsset,
    PackTextAsset,
)

ALLOWED_TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".json"}
ALLOWED_BINARY_SUFFIXES = {".sig"}
# Informative root docs may exist without being prompt context.
ALLOWED_UNDECLARED_NAMES = {
    "readme.md",
    "license",
    "license.md",
    "license.txt",
    "notice",
    "notice.md",
    "changelog.md",
    "distribution.md",
    "context-manifest.yaml",
    ".gitignore",
}
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024  # 25 MiB compressed
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MiB
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB per file
MAX_FILE_COUNT = 200
MANIFEST_NAME = "manifest.yaml"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_relpath(raw: str) -> str:
    path = (raw or "").replace("\\", "/").strip()
    if not path or path.startswith("/") or path.startswith("../") or "/../" in f"/{path}/":
        raise PackLoadError(f"unsafe pack path: {raw!r}")
    if path == ".." or path.endswith("/.."):
        raise PackLoadError(f"unsafe pack path: {raw!r}")
    parts = [p for p in path.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise PackLoadError(f"unsafe pack path: {raw!r}")
    return "/".join(parts)


def _suffix_ok(relpath: str, *, allow_sig: bool = False) -> bool:
    suffix = Path(relpath).suffix.lower()
    if suffix in ALLOWED_TEXT_SUFFIXES:
        return True
    if allow_sig and suffix in ALLOWED_BINARY_SUFFIXES:
        return True
    return False


def _read_yaml(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackLoadError(f"invalid UTF-8: {exc}") from exc
    return yaml.safe_load(text)


def _canonical_digest(parts: list[tuple[str, bytes]]) -> str:
    """SHA-256 over sorted relative paths and their raw bytes.

    Signature (``.sig``) bytes are excluded so a future verifier can sign this
    digest without including the signature itself.
    """
    h = hashlib.sha256()
    for relpath, data in sorted(parts, key=lambda item: item[0]):
        if Path(relpath).suffix.lower() == ".sig":
            continue
        enc_path = relpath.encode("utf-8")
        h.update(len(enc_path).to_bytes(4, "big"))
        h.update(enc_path)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def _declared_paths(manifest: EnrichmentPackManifest) -> set[str]:
    paths: set[str] = set()
    for value in (
        manifest.prompt.domain,
        manifest.prompt.terminology,
        manifest.prompt.edgeCases,
        manifest.context.company,
        manifest.context.dataDomains,
        manifest.context.columnGlossary,
        manifest.context.profilingGuidance,
        manifest.context.classification,
    ):
        if value:
            paths.add(_normalize_relpath(value))
    for rel in list(manifest.prompt.normal or []) + list(manifest.prompt.shared or []):
        paths.add(_normalize_relpath(rel))
    for _kind, rels in (manifest.prompt.stages or {}).items():
        for rel in rels or []:
            paths.add(_normalize_relpath(rel))
    for example in manifest.examples:
        paths.add(_normalize_relpath(example.input))
        paths.add(_normalize_relpath(example.expectedDelta))
    if manifest.evals and manifest.evals.cases:
        paths.add(_normalize_relpath(manifest.evals.cases))
    if manifest.authenticity and manifest.authenticity.signatureFile:
        paths.add(_normalize_relpath(manifest.authenticity.signatureFile))
    return paths


def _load_manifest(raw: dict[str, Any]) -> EnrichmentPackManifest:
    try:
        return EnrichmentPackManifest.model_validate(raw)
    except Exception as exc:
        raise PackValidationError(f"manifest validation failed: {exc}", errors=[str(exc)]) from exc


def _parse_text_assets(
    manifest: EnrichmentPackManifest,
    files: dict[str, bytes],
) -> tuple[dict[str, PackTextAsset], dict[str, PackBinaryAsset], list[str]]:
    declared = _declared_paths(manifest)
    texts: dict[str, PackTextAsset] = {}
    binaries: dict[str, PackBinaryAsset] = {}
    warnings: list[str] = []

    undeclared = sorted(set(files) - declared - {MANIFEST_NAME})
    blocked = [
        path for path in undeclared
        if Path(path).name.lower() not in ALLOWED_UNDECLARED_NAMES
    ]
    if blocked:
        # Fail closed on unexpected content so paid/local packs stay intentional.
        raise PackValidationError(
            "undeclared pack files are not allowed",
            errors=[f"undeclared file: {path}" for path in blocked],
        )

    missing = sorted(declared - set(files))
    if missing:
        raise PackValidationError(
            "manifest references missing assets",
            errors=[f"missing asset: {path}" for path in missing],
        )

    for relpath, data in files.items():
        if relpath == MANIFEST_NAME:
            continue
        allow_sig = bool(
            manifest.authenticity
            and relpath == _normalize_relpath(manifest.authenticity.signatureFile)
        )
        if not _suffix_ok(relpath, allow_sig=allow_sig):
            raise PackValidationError(
                f"unsupported pack file extension: {relpath}",
                errors=[f"unsupported extension: {relpath}"],
            )
        if allow_sig and Path(relpath).suffix.lower() == ".sig":
            binaries[relpath] = PackBinaryAsset(
                relpath=relpath, data=data, sha256=_sha256_bytes(data)
            )
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackLoadError(f"invalid UTF-8 in {relpath}: {exc}") from exc
        if "\x00" in text:
            raise PackLoadError(f"binary content not allowed in text asset: {relpath}")
        texts[relpath] = PackTextAsset(
            relpath=relpath, text=text, sha256=_sha256_bytes(data)
        )
    return texts, binaries, warnings


def _load_glossary(texts: dict[str, PackTextAsset], relpath: Optional[str]) -> list[GlossaryEntry]:
    if not relpath:
        return []
    asset = texts.get(relpath)
    if asset is None:
        raise PackValidationError(f"missing glossary: {relpath}")
    raw = yaml.safe_load(asset.text) or {}
    try:
        doc = GlossaryDocument.model_validate(raw)
    except Exception as exc:
        raise PackValidationError(f"glossary validation failed: {exc}", errors=[str(exc)]) from exc
    return list(doc.columns)


def _load_yaml_asset(texts: dict[str, PackTextAsset], relpath: str) -> Any:
    asset = texts.get(relpath)
    if asset is None:
        raise PackValidationError(f"missing asset: {relpath}")
    return yaml.safe_load(asset.text)


def _assemble_loaded(
    *,
    manifest: EnrichmentPackManifest,
    manifest_bytes: bytes,
    files: dict[str, bytes],
    source_kind: str,
    source_name: str,
) -> LoadedEnrichmentPack:
    texts, binaries, warnings = _parse_text_assets(manifest, files)
    digest_parts = [(MANIFEST_NAME, manifest_bytes)] + [
        (path, data) for path, data in files.items() if path != MANIFEST_NAME
    ]
    sha256 = _canonical_digest(digest_parts)

    glossary = _load_glossary(texts, manifest.context.columnGlossary)

    golden_inputs: dict[str, dict[str, Any]] = {}
    golden_deltas: dict[str, dict[str, Any]] = {}
    for example in manifest.examples:
        raw_in = _load_yaml_asset(texts, _normalize_relpath(example.input))
        raw_delta = _load_yaml_asset(texts, _normalize_relpath(example.expectedDelta))
        if not isinstance(raw_in, dict):
            raise PackValidationError(f"golden input must be a mapping: {example.input}")
        if not isinstance(raw_delta, dict):
            raise PackValidationError(
                f"golden expected delta must be a mapping: {example.expectedDelta}"
            )
        golden_inputs[example.id] = raw_in
        golden_deltas[example.id] = raw_delta

    eval_cases: list[dict[str, Any]] = []
    if manifest.evals and manifest.evals.cases:
        raw_evals = _load_yaml_asset(texts, _normalize_relpath(manifest.evals.cases))
        if not isinstance(raw_evals, dict):
            raise PackValidationError("evals/cases.yaml must be a mapping")
        cases = raw_evals.get("cases") or []
        if not isinstance(cases, list):
            raise PackValidationError("evals.cases must be a list")
        eval_cases = [c for c in cases if isinstance(c, dict)]

    signature_status = "unsigned"
    if manifest.authenticity:
        sig_path = _normalize_relpath(manifest.authenticity.signatureFile)
        if sig_path in binaries:
            signature_status = "unsupported"  # present but not cryptographically verified in v1
            warnings.append(
                "authenticity signature present but verification is not implemented in v1"
            )
        else:
            raise PackValidationError(f"missing authenticity signature file: {sig_path}")

    return LoadedEnrichmentPack(
        manifest=manifest,
        source_kind=source_kind,
        source_name=source_name,
        sha256=sha256,
        texts=texts,
        binaries=binaries,
        glossary=glossary,
        golden_inputs=golden_inputs,
        golden_deltas=golden_deltas,
        eval_cases=eval_cases,
        signature_status=signature_status,
        warnings=warnings,
    )


def _load_folder(root: Path) -> LoadedEnrichmentPack:
    if root.is_symlink():
        raise PackLoadError("pack folder must not be a symlink")
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PackLoadError(f"manifest.yaml not found in {root}")
    if manifest_path.is_symlink():
        raise PackLoadError("manifest.yaml must not be a symlink")

    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > MAX_FILE_BYTES:
        raise PackLoadError("manifest.yaml exceeds maximum file size")
    raw = _read_yaml(manifest_bytes)
    if not isinstance(raw, dict):
        raise PackValidationError("manifest.yaml must be a mapping")
    manifest = _load_manifest(raw)
    declared = _declared_paths(manifest)

    files: dict[str, bytes] = {MANIFEST_NAME: manifest_bytes}
    file_count = 1
    total = len(manifest_bytes)
    for rel in sorted(declared):
        path = root / rel
        if path.is_symlink():
            raise PackLoadError(f"symlinks are not allowed: {rel}")
        if not path.is_file():
            raise PackValidationError(f"missing asset: {rel}", errors=[f"missing asset: {rel}"])
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise PackLoadError(f"file exceeds size limit: {rel}")
        file_count += 1
        total += len(data)
        if file_count > MAX_FILE_COUNT:
            raise PackLoadError("pack exceeds maximum file count")
        if total > MAX_UNCOMPRESSED_BYTES:
            raise PackLoadError("pack exceeds maximum uncompressed size")
        files[rel] = data

    # Include allowed root metadata in the digest for folder/ZIP parity.
    for meta_name in sorted(ALLOWED_UNDECLARED_NAMES):
        meta_path = root / meta_name
        # Also try case variants commonly present
        if not meta_path.is_file():
            matches = [
                p for p in root.iterdir()
                if p.is_file() and p.name.lower() == meta_name
            ]
            meta_path = matches[0] if matches else meta_path
        if meta_path.is_file() and not meta_path.is_symlink():
            # Canonicalize allowed metadata names so folder/ZIP digests match
            # across case-preserving filesystems.
            rel = Path(meta_path.name).name.lower()
            if rel not in files:
                data = meta_path.read_bytes()
                if len(data) > MAX_FILE_BYTES:
                    raise PackLoadError(f"file exceeds size limit: {rel}")
                files[rel] = data

    # Detect undeclared files in tree (shallow walk of pack root).
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.is_symlink():
            raise PackLoadError(f"symlinks are not allowed: {path}")
        rel = _normalize_relpath(str(path.relative_to(root)))
        if rel == MANIFEST_NAME or rel in declared:
            continue
        if path.name.startswith("."):
            continue
        if path.name.lower() in ALLOWED_UNDECLARED_NAMES:
            continue
        raise PackValidationError(
            "undeclared pack files are not allowed",
            errors=[f"undeclared file: {rel}"],
        )

    return _assemble_loaded(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        files=files,
        source_kind="folder",
        source_name=root.name,
    )


def _zip_member_relpath(name: str, *, strip_root: Optional[str]) -> Optional[str]:
    path = name.replace("\\", "/")
    if path.endswith("/"):
        return None
    if path.startswith("/") or path.startswith("../") or "/../" in f"/{path}/":
        raise PackLoadError(f"zip-slip rejected: {name!r}")
    if strip_root:
        prefix = strip_root.rstrip("/") + "/"
        if path == strip_root.rstrip("/"):
            return None
        if path.startswith(prefix):
            path = path[len(prefix) :]
        else:
            return None
    return _normalize_relpath(path)


def _detect_zip_root(names: list[str]) -> Optional[str]:
    """If all members share a single top-level directory, strip it."""
    tops: set[str] = set()
    for name in names:
        path = name.replace("\\", "/").strip("/")
        if not path:
            continue
        tops.add(path.split("/", 1)[0])
    if len(tops) == 1:
        only = next(iter(tops))
        # Only treat as root when manifest is nested under it.
        nested = f"{only}/{MANIFEST_NAME}"
        if nested in {n.replace("\\", "/") for n in names}:
            return only
    return None


def _load_zip(path: Path) -> LoadedEnrichmentPack:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise PackLoadError("ZIP archive exceeds maximum compressed size")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PackLoadError(f"invalid ZIP archive: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILE_COUNT * 2:
            raise PackLoadError("ZIP archive has too many members")
        names = [i.filename for i in infos]
        strip_root = _detect_zip_root(names)

        files: dict[str, bytes] = {}
        total = 0
        file_count = 0
        for info in infos:
            # Reject symlinks / special files (Unix external attributes).
            attrs = (info.external_attr >> 16) & 0o170000
            if attrs in (0o120000, 0o10000, 0o60000, 0o20000):  # symlink, fifo, blk, chr
                raise PackLoadError(f"special ZIP member rejected: {info.filename!r}")
            if info.is_dir():
                continue
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise PackLoadError(f"unsupported ZIP compression: {info.filename!r}")
            if info.file_size > MAX_FILE_BYTES:
                raise PackLoadError(f"ZIP member exceeds size limit: {info.filename!r}")
            rel = _zip_member_relpath(info.filename, strip_root=strip_root)
            if rel is None:
                continue
            if Path(rel).suffix.lower() in {".zip", ".tar", ".gz", ".tgz"}:
                raise PackLoadError(f"nested archives are not allowed: {rel}")
            data = zf.read(info)
            if len(data) != info.file_size:
                # Defensive: zip bombs / lying headers.
                pass
            if len(data) > MAX_FILE_BYTES:
                raise PackLoadError(f"ZIP member exceeds size limit: {rel}")
            file_count += 1
            total += len(data)
            if file_count > MAX_FILE_COUNT:
                raise PackLoadError("pack exceeds maximum file count")
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PackLoadError("pack exceeds maximum uncompressed size")
            if rel in files:
                raise PackLoadError(f"duplicate ZIP member path: {rel}")
            files[rel] = data

        if MANIFEST_NAME not in files:
            raise PackLoadError("manifest.yaml not found in ZIP")
        manifest_bytes = files[MANIFEST_NAME]
        raw = _read_yaml(manifest_bytes)
        if not isinstance(raw, dict):
            raise PackValidationError("manifest.yaml must be a mapping")
        manifest = _load_manifest(raw)
        declared = _declared_paths(manifest)
        # Align digest inputs with folder loader: only declared + manifest +
        # allowed root metadata files.
        filtered: dict[str, bytes] = {MANIFEST_NAME: manifest_bytes}
        for rel, data in files.items():
            if rel == MANIFEST_NAME:
                continue
            name = Path(rel).name.lower()
            if rel in declared:
                filtered[rel] = data
            elif name in ALLOWED_UNDECLARED_NAMES and "/" not in rel.strip("/"):
                filtered[name] = data
        return _assemble_loaded(
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            files=filtered,
            source_kind="zip",
            source_name=path.name,
        )


def load_enrichment_pack(path: Union[str, Path]) -> LoadedEnrichmentPack:
    """Load and parse an enrichment pack from a folder or ZIP path.

    Structural/semantic validation beyond load-time checks should call
    :func:`redibis.enrich.packs.validator.validate_loaded_pack`.
    """
    target = Path(path).expanduser()
    if not target.exists():
        raise PackLoadError(f"pack path not found: {target}")
    if target.is_dir():
        return _load_folder(target)
    if target.is_file() and target.suffix.lower() == ".zip":
        return _load_zip(target)
    raise PackLoadError("pack path must be a directory or .zip archive")


def pack_inspect_summary(pack: LoadedEnrichmentPack) -> dict[str, Any]:
    """Marketing/ops summary without proprietary glossary/prompt content."""
    domains = sorted({e.domain for e in pack.glossary if e.domain})
    return {
        "identity": pack.manifest.release_identity,
        "publisher_id": pack.manifest.metadata.publisherId,
        "name": pack.manifest.metadata.name,
        "version": pack.manifest.metadata.version,
        "display_name": pack.manifest.metadata.displayName,
        "vendor": pack.manifest.metadata.vendor,
        "description": pack.manifest.metadata.description,
        "license": pack.manifest.metadata.license,
        "homepage": pack.manifest.metadata.homepage,
        "support": pack.manifest.metadata.support,
        "compatibility": {
            "redibis": pack.manifest.compatibility.redibis,
            "output_contract": pack.manifest.compatibility.outputContract,
        },
        "sha256": pack.sha256,
        "signature_status": pack.signature_status,
        "source_kind": pack.source_kind,
        "source_name": pack.source_name,
        "domains": domains,
        "counts": {
            "glossary_entries": len(pack.glossary),
            "examples": len(pack.manifest.examples),
            "evaluation_cases": len(pack.eval_cases),
            "edge_case_docs": 1 if pack.manifest.prompt.edgeCases else 0,
        },
        "limits": {
            "glossary_top_k": pack.manifest.selection.glossaryTopK,
            "examples_top_k": pack.manifest.selection.examplesTopK,
            "max_context_characters": pack.manifest.selection.maxContextCharacters,
        },
        "warnings": list(pack.warnings),
    }


def dump_canonical_json(payload: dict[str, Any]) -> bytes:
    """Serialize a reduction-plan payload for hashing."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
