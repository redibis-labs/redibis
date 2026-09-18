"""Server-side sample data library — browse and load files without uploading.

Every path the web app is allowed to read must resolve inside one of the
configured roots. This module is the only place that decides that, so the
containment check exists once and the API layer cannot bypass it.

Threat model: any signed-in web app user can name a relative path. Assume the
path is hostile. The rules, in order:

1. The root is resolved once (``Path.resolve()``, symlinks followed).
2. The candidate is resolved the same way, so a symlink pointing outside the
   root resolves outside it and is rejected — checking the un-resolved path
   would let ``samples/evil -> /etc`` through.
3. No path component may start with ``.`` — no dotfiles, and ``..`` can never
   survive resolution anyway.
4. The suffix must be in the configured allowlist.
5. The file must be a regular file within the size cap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

ENV_ROOT = "REDIBIS_SAMPLE_DATA_DIR"


class SampleDataError(ValueError):
    """Raised when a sample data root or path cannot be used."""


class SampleDataDisabled(SampleDataError):
    """Raised when no sample data root is configured."""


@dataclass(frozen=True)
class SampleRoot:
    name: str
    label: str
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "path": str(self.path),
            "exists": self.path.is_dir(),
        }


def _slug(value: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in value.strip().lower())
    return out.strip("-") or "root"


def _env_roots() -> list[str]:
    raw = os.getenv(ENV_ROOT, "").strip()
    if not raw:
        return []
    # ';' on Windows, ':' elsewhere — but a Windows drive letter uses ':' too,
    # so only split on ':' when it is not a drive separator.
    if ";" in raw or (os.name == "nt"):
        parts = raw.split(";")
    else:
        parts = raw.split(":")
    return [p.strip() for p in parts if p.strip()]


def _config() -> Any:
    from redibis.config import RedibisConfig

    path = os.environ.get("REDIBIS_CONFIG")
    if path:
        try:
            return RedibisConfig.from_yaml(path).sample_data
        except Exception:
            pass
    return RedibisConfig().sample_data


def default_root_path() -> Path | None:
    """Static library: ``redibis/webapp/samples`` next to the web app."""
    try:
        return (Path(__file__).resolve().parents[1] / "webapp" / "samples").expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _default_root() -> str | None:
    """``webapp/samples`` — used when nothing is configured."""
    path = default_root_path()
    return str(path) if path is not None else None


def _is_absolute_user_path(raw: str) -> bool:
    if raw.startswith("/") or raw.startswith("\\"):
        return True
    # Windows drive letter: C:/... or C:\...
    return len(raw) >= 3 and raw[0].isalpha() and raw[1] == ":"


def list_roots(cfg: Any = None) -> list[SampleRoot]:
    """Configured roots, resolved. Env entries come first and are named env1…N.

    When neither ``REDIBIS_SAMPLE_DATA_DIR`` nor ``sample_data.roots`` is set,
    the web app ``samples/`` folder is the root.
    """
    cfg = cfg if cfg is not None else _config()
    entries: list[tuple[str, str, str]] = []
    for index, raw in enumerate(_env_roots(), start=1):
        entries.append((f"env{index}" if index > 1 else "env", Path(raw).name or raw, raw))
    for index, raw in enumerate(list(getattr(cfg, "roots", []) or []), start=1):
        if isinstance(raw, str):
            entries.append((_slug(Path(raw).name or f"root{index}"), Path(raw).name or raw, raw))
        elif isinstance(raw, dict) and raw.get("path"):
            path = str(raw["path"])
            name = _slug(str(raw.get("name") or Path(path).name or f"root{index}"))
            entries.append((name, str(raw.get("label") or Path(path).name or name), path))
    if not entries:
        samples = _default_root()
        if samples:
            entries.append(("samples", "samples", samples))

    out: list[SampleRoot] = []
    seen: set[str] = set()
    for name, label, raw_path in entries:
        try:
            resolved = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique = name
        suffix = 2
        while any(r.name == unique for r in out):
            unique = f"{name}-{suffix}"
            suffix += 1
        out.append(SampleRoot(name=unique, label=label, path=resolved))
    return out


def is_enabled(cfg: Any = None) -> bool:
    return bool(list_roots(cfg))


def get_root(name: str = "", cfg: Any = None) -> SampleRoot:
    roots = list_roots(cfg)
    if not roots:
        raise SampleDataDisabled(
            "no sample data root is configured — place CSV files in "
            "redibis/webapp/samples, set sample_data.roots in REDIBIS_CONFIG, "
            f"or the {ENV_ROOT} environment variable"
        )
    if not name:
        return roots[0]
    for root in roots:
        if root.name == name:
            return root
    raise SampleDataError(f"unknown sample data root {name!r}")


def _allowed_suffixes(cfg: Any) -> set[str]:
    return {
        str(s).lower() if str(s).startswith(".") else "." + str(s).lower()
        for s in (getattr(cfg, "extensions", None) or [])
    }


def _walk(root: Path, *, max_depth: int, max_entries: int) -> Iterator[tuple[Path, int]]:
    """Yield (path, depth) breadth-first, skipping dot-entries. Never follows
    a directory symlink — that is a containment risk and an infinite-loop risk."""
    queue: list[tuple[Path, int]] = [(root, 0)]
    emitted = 0
    while queue and emitted < max_entries:
        current, depth = queue.pop(0)
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (OSError, PermissionError):
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_symlink():
                continue
            if child.is_dir():
                if depth < max_depth:
                    queue.append((child, depth + 1))
                continue
            yield child, depth
            emitted += 1
            if emitted >= max_entries:
                return


def list_files(root_name: str = "", cfg: Any = None) -> dict[str, Any]:
    """Listing for the picker. Never raises on an unreadable subdirectory."""
    cfg = cfg if cfg is not None else _config()
    root = get_root(root_name, cfg)
    suffixes = _allowed_suffixes(cfg)
    max_bytes = int(getattr(cfg, "max_file_mb", 512)) * 1024 * 1024
    files: list[dict[str, Any]] = []
    truncated = False
    max_entries = int(getattr(cfg, "max_entries", 2000))

    if root.path.is_dir():
        count = 0
        for path, _depth in _walk(
            root.path,
            max_depth=int(getattr(cfg, "max_depth", 4)),
            max_entries=max_entries,
        ):
            count += 1
            if path.suffix.lower() not in suffixes:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(root.path).as_posix()
            files.append({
                "path": rel,
                "name": path.name,
                "table": path.stem,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
                "too_large": stat.st_size > max_bytes,
            })
        truncated = count >= max_entries

    return {
        "root": root.name,
        "label": root.label,
        "root_path": str(root.path),
        "roots": [r.to_dict() for r in list_roots(cfg)],
        "files": files,
        "truncated": truncated,
        "max_file_mb": int(getattr(cfg, "max_file_mb", 512)),
        "extensions": sorted(suffixes),
        "allow_scan": bool(getattr(cfg, "allow_scan", True)),
    }


def _resolve_inside_root(root: SampleRoot, rel_parts: Sequence[str], cfg: Any) -> Path:
    """Containment + suffix + size checks for a path already known to be under ``root``."""
    candidate = (root.path / Path(*rel_parts)).resolve()
    try:
        candidate.relative_to(root.path)
    except ValueError:
        raise SampleDataError("path escapes the sample data root") from None
    if not candidate.is_file():
        raise SampleDataError(f"not a file: {'/'.join(rel_parts)}")
    if candidate.suffix.lower() not in _allowed_suffixes(cfg):
        raise SampleDataError(f"unsupported file type: {candidate.suffix or '(none)'}")
    max_bytes = int(getattr(cfg, "max_file_mb", 512)) * 1024 * 1024
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise SampleDataError(f"cannot stat file: {exc}") from exc
    if size > max_bytes:
        raise SampleDataError(
            f"file is {size // (1024 * 1024)} MB, over the "
            f"{max_bytes // (1024 * 1024)} MB sample_data.max_file_mb limit"
        )
    return candidate


def resolve(root_name: str, rel_path: str, cfg: Any = None) -> Path:
    """Resolve a sample path inside a configured root, or raise.

    Accepts a name relative to the root (``customers.csv``) or an absolute
    path that still resolves inside a configured root. This is the security
    gate. Callers must never build a path themselves.
    """
    cfg = cfg if cfg is not None else _config()
    raw = (rel_path or "").strip().replace("\\", "/")
    if not raw:
        raise SampleDataError("path is required")

    if _is_absolute_user_path(raw):
        try:
            absolute = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise SampleDataError(f"cannot resolve path: {exc}") from exc
        roots = [get_root(root_name, cfg)] if root_name else list_roots(cfg)
        for root in roots:
            try:
                relative = absolute.relative_to(root.path)
            except ValueError:
                continue
            parts = [p for p in relative.as_posix().split("/") if p not in ("", ".")]
            if not parts:
                raise SampleDataError("path is required")
            if any(p.startswith(".") for p in parts):
                raise SampleDataError("path may not contain dot entries")
            return _resolve_inside_root(root, parts, cfg)
        raise SampleDataError("path is outside the sample data root")

    root = get_root(root_name, cfg)
    parts = [p for p in raw.lstrip("/").split("/") if p not in ("", ".")]
    if not parts:
        raise SampleDataError("path is required")
    if any(p.startswith(".") for p in parts):
        raise SampleDataError("path may not contain dot entries")
    return _resolve_inside_root(root, parts, cfg)


def read_bytes(root_name: str, rel_path: str, cfg: Any = None) -> tuple[str, bytes]:
    path = resolve(root_name, rel_path, cfg)
    return path.name, path.read_bytes()


def _containing_root(path: Path, cfg: Any, root_name: str = "") -> SampleRoot:
    if root_name:
        return get_root(root_name, cfg)
    for root in list_roots(cfg):
        try:
            path.relative_to(root.path)
            return root
        except ValueError:
            continue
    raise SampleDataError("path is outside the sample data root")


def preview(
    root_name: str,
    rel_path: str,
    *,
    rows: int = 20,
    cfg: Any = None,
) -> dict[str, Any]:
    """Column names, dtypes and a few rows — enough to populate the scan form."""
    path = resolve(root_name, rel_path, cfg)
    root = _containing_root(path, cfg, root_name)
    relative = path.relative_to(root.path).as_posix()
    frame, total = _read_head(path, rows)
    columns = [str(c) for c in frame.columns]
    return {
        "root": root.name,
        "path": relative,
        "name": path.name,
        "table": path.stem,
        "columns": columns,
        "dtypes": {str(c): str(frame[c].dtype) for c in frame.columns},
        "rows": [
            [("" if v is None else str(v)) for v in record]
            for record in frame.head(rows).itertuples(index=False, name=None)
        ],
        "row_count": total,
        "row_count_exact": total is not None,
        "size": path.stat().st_size,
    }


def _read_head(path: Path, rows: int):
    import pandas as pd

    suffix = path.suffix.lower()
    if suffix == ".parquet":
        frame = pd.read_parquet(path)
        return frame.head(rows), int(len(frame))
    if suffix in (".xlsx", ".xls"):
        frame = pd.read_excel(path, nrows=rows)
        return frame, None
    sep = "\t" if suffix == ".tsv" else ","
    frame = pd.read_csv(path, sep=sep, nrows=rows, dtype=str, keep_default_na=False)
    return frame, _count_csv_rows(path)


def _count_csv_rows(path: Path, *, cap_bytes: int = 64 * 1024 * 1024) -> Optional[int]:
    """Exact data-row count for a CSV, or None when the file is too big to sweep."""
    try:
        if path.stat().st_size > cap_bytes:
            return None
        with path.open("rb") as handle:
            lines = sum(1 for _ in handle)
    except OSError:
        return None
    return max(0, lines - 1)
