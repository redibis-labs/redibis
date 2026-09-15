"""Versioned Text Gateway use-case assets.

A use case is an operator-authored fixture (raw text + expected/forbidden
spans + rule edits). It lives under the configs dir, never in the run
registry — the run store continues to persist HMAC digests only.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Union

from redibis.pii.eval.builder import CorpusBuildError, locate_unique_value

PathLike = Union[str, Path]

KIND = "redibis.text_usecase"
SCHEMA_VERSION = "1.0"
_SAFE_ID = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


class UseCaseValidationError(ValueError):
    """Raised when a span value cannot be uniquely located in the text."""

    def __init__(self, message: str, *, span_id: str = "", value: str = ""):
        super().__init__(message)
        self.span_id = span_id
        self.value = value


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_usecase_dir() -> Path:
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "pii_usecases"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _safe_id(uc_id: str) -> str:
    raw = str(uc_id or "").strip()
    if not raw or any(ch not in _SAFE_ID for ch in raw) or ".." in raw:
        raise UseCaseValidationError(f"invalid use-case id {uc_id!r}")
    return raw


def _as_tuple_maps(raw: Any) -> tuple[dict, ...]:
    if not raw:
        return ()
    out: list[dict] = []
    for item in raw:
        if isinstance(item, Mapping):
            out.append(dict(item))
    return tuple(out)


def _as_tags(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        tag = str(item).strip()
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        out.append(tag)
    return tuple(out)


def validate_spans(
    text: str,
    spans: Any,
    *,
    kind: str = "expected",
) -> tuple[tuple[dict, ...], list[dict]]:
    """Ensure each span's ``value`` equals ``text[start:end]``, relocating uniquely."""
    relocated: list[dict] = []
    out: list[dict] = []
    for index, raw in enumerate(spans or ()):
        if not isinstance(raw, Mapping):
            continue
        span = dict(raw)
        sid = str(span.get("id") or f"s{index + 1}")
        span["id"] = sid
        value = span.get("value")
        start, end = span.get("start"), span.get("end")
        if value is not None:
            value = str(value)
        try:
            if start is not None and end is not None:
                start_i, end_i = int(start), int(end)
                slice_ = text[start_i:end_i]
                if value in (None, ""):
                    span["value"] = slice_
                    span["start"], span["end"] = start_i, end_i
                elif value == slice_:
                    span["start"], span["end"] = start_i, end_i
                    span["value"] = value
                else:
                    loc_s, loc_e = locate_unique_value(text, value)
                    span["start"], span["end"] = loc_s, loc_e
                    span["value"] = value
                    relocated.append({
                        "id": sid,
                        "kind": kind,
                        "start": loc_s,
                        "end": loc_e,
                        "note": "relocated",
                    })
            elif value:
                loc_s, loc_e = locate_unique_value(text, value)
                span["start"], span["end"] = loc_s, loc_e
                span["value"] = value
                relocated.append({
                    "id": sid,
                    "kind": kind,
                    "start": loc_s,
                    "end": loc_e,
                    "note": "relocated",
                })
            else:
                raise UseCaseValidationError(
                    f"{sid}: missing value and offsets",
                    span_id=sid,
                    value="",
                )
        except CorpusBuildError as exc:
            raise UseCaseValidationError(
                f"{sid}: {exc} (value={value!r})",
                span_id=sid,
                value=str(value or ""),
            ) from exc
        out.append(span)
    return tuple(out), relocated


@dataclass(frozen=True)
class UseCase:
    id: str
    version: int
    name: str
    created: str
    updated: str
    author: str
    text: str
    language: str = "ar"
    tags: tuple[str, ...] = ()
    context: dict = field(default_factory=dict)
    expected_spans: tuple[dict, ...] = ()
    forbidden_spans: tuple[dict, ...] = ()
    rule_edits: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "created": self.created,
            "updated": self.updated,
            "author": self.author,
            "language": self.language,
            "tags": list(self.tags),
            "text": self.text,
            "context": dict(self.context or {}),
            "expected_spans": [dict(s) for s in self.expected_spans],
            "forbidden_spans": [dict(s) for s in self.forbidden_spans],
            "rule_edits": dict(self.rule_edits or {}),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "UseCase":
        raw = dict(data or {})
        return cls(
            id=str(raw.get("id") or ""),
            version=int(raw.get("version") or 0),
            name=str(raw.get("name") or ""),
            created=str(raw.get("created") or ""),
            updated=str(raw.get("updated") or ""),
            author=str(raw.get("author") or ""),
            text=str(raw.get("text") or ""),
            language=str(raw.get("language") or "ar"),
            tags=_as_tags(raw.get("tags")),
            context=dict(raw.get("context") or {}),
            expected_spans=_as_tuple_maps(raw.get("expected_spans")),
            forbidden_spans=_as_tuple_maps(raw.get("forbidden_spans")),
            rule_edits=dict(raw.get("rule_edits") or {}),
            notes=str(raw.get("notes") or ""),
        )


def usecase_from_payload(
    data: Mapping[str, Any],
    *,
    existing: UseCase | None = None,
    author: str = "",
) -> tuple[UseCase, list[dict]]:
    """Validate spans and build a UseCase ready for ``save()`` (version 0 = mint)."""
    raw = dict(data or {})
    text = str(raw.get("text") if raw.get("text") is not None else (existing.text if existing else ""))
    expected, relocated_e = validate_spans(text, raw.get("expected_spans"), kind="expected")
    forbidden, relocated_f = validate_spans(text, raw.get("forbidden_spans"), kind="forbidden")
    now = _utc_now()
    uc_id = str(raw.get("id") or (existing.id if existing else "") or ("uc_" + uuid.uuid4().hex[:12]))
    created = str(raw.get("created") or (existing.created if existing else now))
    return UseCase(
        id=_safe_id(uc_id),
        version=0,
        name=str(raw.get("name") or (existing.name if existing else "") or "untitled"),
        created=created,
        updated=now,
        author=str(raw.get("author") or author or (existing.author if existing else "")),
        text=text,
        language=str(raw.get("language") or (existing.language if existing else "ar") or "ar"),
        tags=_as_tags(raw.get("tags") if "tags" in raw else (existing.tags if existing else ())),
        context=dict(raw.get("context") if raw.get("context") is not None else (existing.context if existing else {})),
        expected_spans=expected,
        forbidden_spans=forbidden,
        rule_edits=dict(
            raw.get("rule_edits")
            if raw.get("rule_edits") is not None
            else (existing.rule_edits if existing else {})
        ),
        notes=str(raw.get("notes") if raw.get("notes") is not None else (existing.notes if existing else "")),
    ), relocated_e + relocated_f


class UseCaseStore:
    """Filesystem store: immutable version files + an index of latest versions."""

    def __init__(self, root: PathLike | None = None):
        self.root = Path(root or default_usecase_dir()).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()

    def _dir(self, uc_id: str) -> Path:
        return self.root / _safe_id(uc_id)

    def _version_path(self, uc_id: str, version: int) -> Path:
        return self._dir(uc_id) / f"v{int(version)}.json"

    def _latest_path(self, uc_id: str) -> Path:
        return self._dir(uc_id) / "latest"

    def _index_path(self) -> Path:
        return self.root / "index.json"

    @contextmanager
    def _index_lock(self) -> Iterator[None]:
        lock_path = self.root / "index.lock"
        lock_path.touch(exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as fh:
            locked = False
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                locked = True
            except Exception:
                pass
            with self._thread_lock:
                yield
            if locked:
                try:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass

    def _load_index(self) -> dict[str, Any]:
        path = self._index_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"use-case index is corrupt at {path}; refusing to wipe history: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"use-case index is corrupt at {path}: not a mapping")
        return raw

    def _save_index(self, index: dict[str, Any]) -> None:
        _atomic_write(
            self._index_path(),
            json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )

    def _read_latest(self, uc_id: str) -> int:
        path = self._latest_path(uc_id)
        if not path.is_file():
            return 0
        try:
            return int(path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            return 0

    def list(self, *, tag: str = "", limit: int = 100) -> list[dict]:
        with self._index_lock():
            index = self._load_index()
        needle = str(tag or "").strip().casefold()
        rows: list[dict] = []
        for uc_id, meta in index.items():
            if not isinstance(meta, Mapping):
                continue
            tags = [str(t) for t in (meta.get("tags") or [])]
            if needle and needle not in {t.casefold() for t in tags}:
                continue
            rows.append({
                "id": uc_id,
                "name": meta.get("name") or "",
                "tags": tags,
                "latest_version": int(meta.get("latest_version") or 0),
                "updated": meta.get("updated") or "",
                "span_count": int(meta.get("span_count") or 0),
                "language": meta.get("language") or "",
            })
        rows.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
        return rows[: max(0, int(limit))]

    def get(self, uc_id: str, version: int | None = None) -> UseCase | None:
        safe = _safe_id(uc_id)
        ver = int(version) if version is not None else self._read_latest(safe)
        if ver <= 0:
            return None
        path = self._version_path(safe, ver)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return UseCase.from_dict(raw)

    def save(self, uc: UseCase) -> UseCase:
        """Persist ``uc`` as the next immutable version."""
        expected, _ = validate_spans(uc.text, uc.expected_spans, kind="expected")
        forbidden, _ = validate_spans(uc.text, uc.forbidden_spans, kind="forbidden")
        safe = _safe_id(uc.id)
        now = _utc_now()
        with self._index_lock():
            latest = self._read_latest(safe)
            version = latest + 1
            stored = UseCase(
                id=safe,
                version=version,
                name=uc.name,
                created=uc.created or now,
                updated=now,
                author=uc.author,
                text=uc.text,
                language=uc.language or "ar",
                tags=tuple(uc.tags),
                context=dict(uc.context or {}),
                expected_spans=expected,
                forbidden_spans=forbidden,
                rule_edits=dict(uc.rule_edits or {}),
                notes=uc.notes,
            )
            _atomic_write(
                self._version_path(safe, version),
                json.dumps(stored.to_dict(), indent=2, ensure_ascii=False) + "\n",
            )
            _atomic_write(self._latest_path(safe), str(version) + "\n")
            index = self._load_index()
            index[safe] = {
                "name": stored.name,
                "latest_version": version,
                "updated": stored.updated,
                "tags": list(stored.tags),
                "span_count": len(stored.expected_spans) + len(stored.forbidden_spans),
                "language": stored.language,
            }
            self._save_index(index)
        return stored

    def create(self, **fields: Any) -> UseCase:
        now = _utc_now()
        uc_id = str(fields.get("id") or "").strip() or ("uc_" + uuid.uuid4().hex[:12])
        expected, _ = validate_spans(str(fields.get("text") or ""), fields.get("expected_spans") or ())
        forbidden, _ = validate_spans(str(fields.get("text") or ""), fields.get("forbidden_spans") or ())
        draft = UseCase(
            id=uc_id,
            version=0,
            name=str(fields.get("name") or "untitled"),
            created=str(fields.get("created") or now),
            updated=now,
            author=str(fields.get("author") or ""),
            text=str(fields.get("text") or ""),
            language=str(fields.get("language") or "ar"),
            tags=_as_tags(fields.get("tags")),
            context=dict(fields.get("context") or {}),
            expected_spans=expected,
            forbidden_spans=forbidden,
            rule_edits=dict(fields.get("rule_edits") or {}),
            notes=str(fields.get("notes") or ""),
        )
        return self.save(draft)

    def delete(self, uc_id: str) -> bool:
        safe = _safe_id(uc_id)
        folder = self._dir(safe)
        with self._index_lock():
            index = self._load_index()
            existed = safe in index or folder.is_dir()
            index.pop(safe, None)
            self._save_index(index)
            if folder.is_dir():
                for path in folder.iterdir():
                    if path.is_file():
                        path.unlink()
                folder.rmdir()
        return existed
