"""Persist span-curation records under the configs dir (never the run registry)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, Union

from redibis.pii.curation import Curation, CurationError, assert_no_surfaces

PathLike = Union[str, Path]

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def default_curation_dir() -> Path:
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "pii_curation"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _safe_run(run_uuid: str) -> str:
    raw = str(run_uuid or "").strip()
    if not raw or not _SAFE.match(raw) or ".." in raw:
        raise CurationError(f"invalid run_uuid {run_uuid!r}")
    return raw


class CurationStore:
    def __init__(self, root: PathLike | None = None):
        self.root = Path(root or default_curation_dir()).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_uuid: str) -> Path:
        return self.root / f"{_safe_run(run_uuid)}.json"

    def put(self, curation: Curation) -> Path:
        payload = curation.to_dict()
        assert_no_surfaces(payload)
        path = self.path_for(curation.run_uuid)
        _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        return path

    def get(self, run_uuid: str) -> Optional[Curation]:
        path = self.path_for(run_uuid)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return Curation.from_dict(raw)
