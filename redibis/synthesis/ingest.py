"""Safe ingestion of requirements documents and pipeline source files.

Never executes uploaded code. ZIP traversal / size / type limits enforced.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

# Defaults — conservative for a first release.
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB per file
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 20 MiB
MAX_ZIP_UNCOMPRESSED = 40 * 1024 * 1024

ALLOWED_SUFFIXES = frozenset({
    ".md", ".txt", ".yaml", ".yml", ".json",
    ".sql",
    ".py", ".scala",
    ".dsx", ".xml",
})

REQUIREMENT_SUFFIXES = frozenset({".md", ".txt", ".yaml", ".yml", ".json"})
SOURCE_SUFFIXES = frozenset({".sql", ".py", ".scala", ".dsx", ".xml"})

# Binary / executable DataStage packages are rejected in v1.
REJECTED_SUFFIXES = frozenset({
    ".exe", ".dll", ".so", ".bin", ".class", ".jar", ".war",
    ".dstx", ".pds", ".o", ".pyc",
})

_SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
]


@dataclass
class IngestedFile:
    relpath: str
    kind: str  # requirements | sql | spark | datastage | other
    sha256: str
    size: int
    text: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relpath": self.relpath,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
            "warnings": list(self.warnings),
            # text omitted from compact manifests by default
        }


@dataclass
class InputManifest:
    files: list[IngestedFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_bytes: int = 0

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        out_files = []
        for f in self.files:
            d = f.to_dict()
            if include_text:
                d["text"] = f.text
            out_files.append(d)
        return {
            "files": out_files,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "total_bytes": self.total_bytes,
            "file_count": len(self.files),
        }


def _classify(path: str) -> str:
    suffix = Path(path).suffix.lower()
    name = Path(path).name.lower()
    if suffix in REQUIREMENT_SUFFIXES:
        if any(tok in name for tok in ("req", "requirement", "traceab", "sla", "nfr")):
            return "requirements"
        return "requirements"
    if suffix == ".sql":
        return "sql"
    if suffix in {".py", ".scala"}:
        return "spark"
    if suffix in {".dsx", ".xml"}:
        return "datastage"
    return "other"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scan_secrets(text: str) -> list[str]:
    hits: list[str] = []
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            hits.append(f"possible secret pattern matched: {pat.pattern[:40]}…")
    return hits


def _decode_text(data: bytes, relpath: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    # Reject obvious binary (NUL bytes).
    if b"\x00" in data[:4096]:
        raise ValueError(f"binary content rejected: {relpath}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        warnings.append(f"non-utf8 bytes replaced in {relpath}")
    warnings.extend(_scan_secrets(text))
    # Redact private key blocks for prompt safety (keep a stub).
    if "PRIVATE KEY-----" in text:
        text = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[REDACTED PRIVATE KEY]",
            text,
            flags=re.S,
        )
        warnings.append(f"private key block redacted in {relpath}")
    return text, warnings


def _safe_zip_member_name(name: str) -> str:
    # Block absolute paths and .. traversal.
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/") or cleaned.startswith("../") or "/../" in cleaned:
        raise ValueError(f"unsafe zip member path: {name}")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"unsafe zip member path: {name}")
    return "/".join(parts)


def _read_path(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes: {path}")
    return data


def ingest_paths(
    paths: Iterable[Union[str, Path]],
    *,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> InputManifest:
    """Ingest local files, directories of allowed files, and ZIP archives into an ``InputManifest``."""
    manifest = InputManifest()

    def _expand(path: Path) -> list[Path]:
        if path.is_file():
            return [path]
        if path.is_dir():
            found: list[Path] = []
            for child in sorted(path.rglob("*")):
                if not child.is_file():
                    continue
                suffix = child.suffix.lower()
                if suffix == ".zip" or suffix in ALLOWED_SUFFIXES:
                    found.append(child)
                elif suffix in REJECTED_SUFFIXES:
                    manifest.errors.append(f"rejected binary/package type: {child}")
            if not found:
                manifest.warnings.append(f"directory contained no accepted files: {path}")
            return found
        return []

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            manifest.errors.append(f"path not found: {path}")
            continue
        for file_path in _expand(path):
            if file_path.suffix.lower() == ".zip":
                _ingest_zip(
                    file_path,
                    manifest,
                    max_files=max_files,
                    max_file_bytes=max_file_bytes,
                    max_total_bytes=max_total_bytes,
                )
                continue
            try:
                _ingest_bytes(
                    relpath=str(file_path.name),
                    data=_read_path(file_path),
                    manifest=manifest,
                    max_files=max_files,
                    max_file_bytes=max_file_bytes,
                    max_total_bytes=max_total_bytes,
                )
            except Exception as exc:
                manifest.errors.append(str(exc))
    return manifest


def ingest_bytes_map(
    items: dict[str, bytes],
    *,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> InputManifest:
    """Ingest an in-memory ``{filename: bytes}`` map (API uploads)."""
    manifest = InputManifest()
    for name, data in items.items():
        try:
            if name.lower().endswith(".zip"):
                _ingest_zip_bytes(
                    data,
                    manifest,
                    max_files=max_files,
                    max_file_bytes=max_file_bytes,
                    max_total_bytes=max_total_bytes,
                )
            else:
                _ingest_bytes(
                    relpath=Path(name).name,
                    data=data,
                    manifest=manifest,
                    max_files=max_files,
                    max_file_bytes=max_file_bytes,
                    max_total_bytes=max_total_bytes,
                )
        except Exception as exc:
            manifest.errors.append(str(exc))
    return manifest


def _ingest_zip(
    path: Path,
    manifest: InputManifest,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> None:
    data = path.read_bytes()
    _ingest_zip_bytes(
        data,
        manifest,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


def _ingest_zip_bytes(
    data: bytes,
    manifest: InputManifest,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid zip archive: {exc}") from exc

    uncompressed = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            uncompressed += int(info.file_size)
            if uncompressed > MAX_ZIP_UNCOMPRESSED:
                raise ValueError("zip uncompressed size exceeds limit")
            rel = _safe_zip_member_name(info.filename)
            member = zf.read(info)
            _ingest_bytes(
                relpath=rel,
                data=member,
                manifest=manifest,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )


def _ingest_bytes(
    *,
    relpath: str,
    data: bytes,
    manifest: InputManifest,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> None:
    if len(manifest.files) >= max_files:
        raise ValueError(f"file count exceeds limit ({max_files})")
    if len(data) > max_file_bytes:
        raise ValueError(f"file exceeds {max_file_bytes} bytes: {relpath}")
    if manifest.total_bytes + len(data) > max_total_bytes:
        raise ValueError(f"total bytes exceed limit ({max_total_bytes})")

    suffix = Path(relpath).suffix.lower()
    if suffix in REJECTED_SUFFIXES:
        raise ValueError(f"rejected file type {suffix}: {relpath}")
    if suffix and suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"unsupported file type {suffix or '(none)'}: {relpath}; "
            f"allowed: {sorted(ALLOWED_SUFFIXES)}"
        )

    text, warnings = _decode_text(data, relpath)
    kind = _classify(relpath)
    ingested = IngestedFile(
        relpath=relpath,
        kind=kind,
        sha256=_sha256(data),
        size=len(data),
        text=text,
        warnings=warnings,
    )
    manifest.files.append(ingested)
    manifest.total_bytes += len(data)
    manifest.warnings.extend(warnings)
