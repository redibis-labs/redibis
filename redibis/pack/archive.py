"""Archive safety limits and zip/tar.gz I/O for ``.rdbpack`` files."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

from redibis.pack.errors import PackLoadError

ALLOWED_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".md"})
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024  # 25 MiB compressed
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MiB
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB per file
MAX_FILE_COUNT = 200

# Fixed DOS date for deterministic ZIP members (1980-01-01).
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def normalize_relpath(raw: str) -> str:
    path = (raw or "").replace("\\", "/").strip()
    if not path or path.startswith("/") or path.startswith("../") or "/../" in f"/{path}/":
        raise PackLoadError(f"unsafe pack path: {raw!r}")
    if path == ".." or path.endswith("/.."):
        raise PackLoadError(f"unsafe pack path: {raw!r}")
    parts = [p for p in path.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise PackLoadError(f"unsafe pack path: {raw!r}")
    return "/".join(parts)


def suffix_allowed(relpath: str) -> bool:
    return Path(relpath).suffix.lower() in ALLOWED_SUFFIXES


def _reject_special_zip_attrs(info: zipfile.ZipInfo) -> None:
    attrs = (info.external_attr >> 16) & 0o170000
    if attrs in (0o120000, 0o10000, 0o60000, 0o20000):  # symlink, fifo, blk, chr
        raise PackLoadError(f"special ZIP member rejected: {info.filename!r}")


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
    return normalize_relpath(path)


def _detect_zip_root(names: list[str], *, manifest_name: str) -> Optional[str]:
    tops: set[str] = set()
    for name in names:
        path = name.replace("\\", "/").strip("/")
        if not path:
            continue
        tops.add(path.split("/", 1)[0])
    if len(tops) == 1:
        only = next(iter(tops))
        nested = f"{only}/{manifest_name}"
        if nested in {n.replace("\\", "/") for n in names}:
            return only
    return None


def read_zip_files(path: Path, *, manifest_name: str) -> dict[str, bytes]:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise PackLoadError("archive exceeds maximum compressed size")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PackLoadError(f"invalid ZIP archive: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILE_COUNT * 2:
            raise PackLoadError("archive has too many members")
        names = [i.filename for i in infos]
        strip_root = _detect_zip_root(names, manifest_name=manifest_name)

        files: dict[str, bytes] = {}
        total = 0
        for info in infos:
            _reject_special_zip_attrs(info)
            if info.is_dir():
                continue
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise PackLoadError(f"unsupported ZIP compression: {info.filename!r}")
            if info.file_size > MAX_FILE_BYTES:
                raise PackLoadError(f"archive member exceeds size limit: {info.filename!r}")
            rel = _zip_member_relpath(info.filename, strip_root=strip_root)
            if rel is None:
                continue
            if Path(rel).suffix.lower() in {".zip", ".tar", ".gz", ".tgz", ".rdbpack"}:
                raise PackLoadError(f"nested archives are not allowed: {rel}")
            if not suffix_allowed(rel):
                raise PackLoadError(f"unsupported pack file extension: {rel}")
            data = zf.read(info)
            if len(data) > MAX_FILE_BYTES:
                raise PackLoadError(f"archive member exceeds size limit: {rel}")
            total += len(data)
            if len(files) + 1 > MAX_FILE_COUNT:
                raise PackLoadError("pack exceeds maximum file count")
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PackLoadError("pack exceeds maximum uncompressed size")
            if rel in files:
                raise PackLoadError(f"duplicate archive member path: {rel}")
            files[rel] = data
        return files


def read_tar_files(path: Path) -> dict[str, bytes]:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise PackLoadError("archive exceeds maximum compressed size")
    try:
        tf = tarfile.open(path, mode="r:*")
    except tarfile.TarError as exc:
        raise PackLoadError(f"invalid tar archive: {exc}") from exc

    with tf:
        files: dict[str, bytes] = {}
        total = 0
        members = tf.getmembers()
        if len(members) > MAX_FILE_COUNT * 2:
            raise PackLoadError("archive has too many members")
        for member in members:
            if not member.isfile():
                if member.issym() or member.islnk():
                    raise PackLoadError(f"symlinks are not allowed: {member.name!r}")
                continue
            if member.size > MAX_FILE_BYTES:
                raise PackLoadError(f"archive member exceeds size limit: {member.name!r}")
            rel = normalize_relpath(member.name)
            if Path(rel).suffix.lower() in {".zip", ".tar", ".gz", ".tgz", ".rdbpack"}:
                raise PackLoadError(f"nested archives are not allowed: {rel}")
            if not suffix_allowed(rel):
                raise PackLoadError(f"unsupported pack file extension: {rel}")
            extracted = tf.extractfile(member)
            if extracted is None:
                raise PackLoadError(f"cannot read archive member: {rel}")
            data = extracted.read()
            if len(data) > MAX_FILE_BYTES:
                raise PackLoadError(f"archive member exceeds size limit: {rel}")
            total += len(data)
            if len(files) + 1 > MAX_FILE_COUNT:
                raise PackLoadError("pack exceeds maximum file count")
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PackLoadError("pack exceeds maximum uncompressed size")
            if rel in files:
                raise PackLoadError(f"duplicate archive member path: {rel}")
            files[rel] = data
        return files


def write_zip_bytes(files: dict[str, bytes]) -> bytes:
    """Write a deterministic ZIP (sorted members, fixed timestamps)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for relpath in sorted(files):
            data = files[relpath]
            if len(data) > MAX_FILE_BYTES:
                raise PackLoadError(f"file exceeds size limit: {relpath}")
            info = zipfile.ZipInfo(filename=relpath, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    payload = buf.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise PackLoadError("archive exceeds maximum compressed size")
    return payload


def write_zip_file(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(write_zip_bytes(files))


def detect_archive_kind(path: Path) -> str:
    if path.is_dir():
        return "folder"
    if not path.is_file():
        raise PackLoadError(f"pack path not found: {path}")
    if zipfile.is_zipfile(path):
        return "zip"
    if tarfile.is_tarfile(path):
        return "tar.gz"
    raise PackLoadError(
        "pack path must be a directory, ZIP (.rdbpack), or tar.gz archive"
    )
