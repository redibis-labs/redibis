"""Path containment for local workspace roots.

Mirrors ``redibis.services.sample_data.resolve``: resolve then contain, no
dot-entries, refuse a folder that aliases the global store or configs dir.
Unlike sample_data this gate is for *directories*, not files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from redibis.workspace.model import WorkspaceDenied

AnyStrLike = str | os.PathLike[str]


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def configs_dir() -> Path:
    raw = os.getenv("REDIBIS_CONFIGS_DIR") or os.getenv("CONFIGS_DIR") or "./configs"
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(raw).expanduser()


def default_storage_root() -> Path:
    raw = os.getenv("LOCAL_STORAGE_ROOT") or "./_local_storage"
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(raw).expanduser()


def resolve_allowed_roots(raw_roots: Sequence[AnyStrLike] | None) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for raw in raw_roots or []:
        text = str(raw).strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceDenied(f"cannot resolve allowed root {text!r}: {exc}") from exc
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def resolve_local_folder(
    folder: str | os.PathLike[str],
    allowed_roots: Sequence[str | os.PathLike[str]] | None,
    *,
    configs: Path | None = None,
    storage_root: Path | None = None,
    must_exist: bool = True,
) -> Path:
    """Resolve *folder* inside an allowed root, or raise WorkspaceDenied.

    Empty ``allowed_roots`` means local workspaces are disabled.
    """
    roots = resolve_allowed_roots(list(allowed_roots or []))
    if not roots:
        raise WorkspaceDenied(
            "local workspaces are disabled — set workspaces.allowed_roots in REDIBIS_CONFIG"
        )

    raw = (str(folder) or "").strip()
    if not raw:
        raise WorkspaceDenied("folder is required")

    try:
        candidate = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceDenied(f"cannot resolve folder: {exc}") from exc

    matched: Path | None = None
    relative = None
    for root in roots:
        try:
            relative = candidate.relative_to(root)
            matched = root
            break
        except ValueError:
            continue
    if matched is None:
        raise WorkspaceDenied("folder is outside workspaces.allowed_roots")

    parts = [p for p in relative.as_posix().split("/") if p not in ("", ".")]
    if any(p.startswith(".") for p in parts):
        raise WorkspaceDenied("path may not contain dot entries")
    # The workspace directory itself must not be a dotfolder even when it *is* the root.
    if candidate.name.startswith("."):
        raise WorkspaceDenied("path may not contain dot entries")

    cfg = configs if configs is not None else configs_dir()
    store = storage_root if storage_root is not None else default_storage_root()
    if _is_inside(candidate, cfg) or candidate == cfg:
        raise WorkspaceDenied("folder is inside the server configs directory")
    if _is_inside(candidate, store) or candidate == store:
        raise WorkspaceDenied("folder aliases the global active store")

    if must_exist and not candidate.is_dir():
        raise WorkspaceDenied(f"not a directory: {candidate}")
    if candidate.exists() and candidate.is_symlink():
        # resolve() already followed it; if it still reports as a symlink that
        # points at itself we already contained the target.
        pass
    if candidate.exists() and not candidate.is_dir():
        raise WorkspaceDenied(f"not a directory: {candidate}")
    return candidate
