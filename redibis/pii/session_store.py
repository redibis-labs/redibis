"""Operator sessions: session_name / date / usecases.

Wraps ``UseCaseStore`` with a root override. Lives under the configs dir,
never the run registry.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, Union

from redibis.pii.usecase_store import UseCase, UseCaseStore, UseCaseValidationError

PathLike = Union[str, Path]
KIND = "redibis.pii_session"
SCHEMA_VERSION = "1.0"
_SAFE = re.compile(r"[^\w\-]")


class SessionError(ValueError):
    """Invalid session name, slug collision, or missing session."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def default_session_dir() -> Path:
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "pii_sessions"


def session_slug(name: str) -> str:
    """Same spirit as ``_sanitize_sample_session_id``: word chars and hyphen only."""
    hyphenated = (name or "").strip().replace(" ", "-")
    safe = _SAFE.sub("", hyphenated)
    if not safe or len(safe) > 64:
        raise SessionError("invalid session name (use letters, digits, hyphen, underscore; max 64)")
    return safe


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class SessionMeta:
    slug: str
    name: str
    created: str
    owner: str = ""
    notes: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "schema_version": SCHEMA_VERSION,
            "slug": self.slug,
            "name": self.name,
            "created": self.created,
            "owner": self.owner,
            "notes": self.notes,
            "defaults": dict(self.defaults or {}),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SessionMeta":
        data = dict(raw or {})
        return cls(
            slug=str(data.get("slug") or ""),
            name=str(data.get("name") or ""),
            created=str(data.get("created") or ""),
            owner=str(data.get("owner") or ""),
            notes=str(data.get("notes") or ""),
            defaults=dict(data.get("defaults") or {}),
        )


class SessionStore:
    def __init__(self, root: PathLike | None = None):
        self.root = Path(root or default_session_dir()).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, slug: str) -> Path:
        return self.root / session_slug(slug)

    def _meta_path(self, slug: str) -> Path:
        return self._dir(slug) / "session.json"

    def _date_dir(self, slug: str, date: str) -> Path:
        day = date or _today()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise SessionError(f"invalid date {date!r}")
        return self._dir(slug) / day

    def list(self) -> list[SessionMeta]:
        rows: list[SessionMeta] = []
        if not self.root.is_dir():
            return rows
        for path in sorted(self.root.iterdir()):
            meta_path = path / "session.json"
            if not meta_path.is_file():
                continue
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                rows.append(SessionMeta.from_dict(raw))
        return rows

    def get(self, slug: str) -> SessionMeta | None:
        path = self._meta_path(slug)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return SessionMeta.from_dict(raw)

    def create(self, name: str, *, owner: str = "", notes: str = "", defaults: dict | None = None) -> SessionMeta:
        slug = session_slug(name)
        dest = self._dir(slug)
        if dest.exists():
            raise SessionError(f"session {slug!r} already exists")
        meta = SessionMeta(
            slug=slug,
            name=str(name or slug),
            created=_utc_now(),
            owner=str(owner or ""),
            notes=str(notes or ""),
            defaults=dict(defaults or {}),
        )
        _atomic_write(self._meta_path(slug), json.dumps(meta.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        return meta

    def dates(self, slug: str) -> list[str]:
        folder = self._dir(slug)
        if not folder.is_dir():
            return []
        return sorted(
            p.name for p in folder.iterdir()
            if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
        )

    def usecases(self, slug: str, *, date: str | None = None) -> UseCaseStore:
        day = date or _today()
        root = self._date_dir(slug, day) / "usecases"
        root.mkdir(parents=True, exist_ok=True)
        return UseCaseStore(root=root)

    def save_usecase(
        self,
        slug: str,
        uc: UseCase,
        *,
        date: str | None = None,
        curation: Any = None,
        llm_verdict: Any = None,
        recommendations: Any = None,
    ) -> UseCase:
        if self.get(slug) is None:
            raise SessionError(f"unknown session {slug!r}")
        day = date or _today()
        store = self.usecases(slug, date=day)
        context = dict(uc.context or {})
        if curation is not None:
            payload = curation.to_dict() if hasattr(curation, "to_dict") else dict(curation)
            context["curation"] = payload
        stored = UseCase(
            id=uc.id,
            version=uc.version,
            name=uc.name,
            created=uc.created,
            updated=uc.updated,
            author=uc.author,
            text=uc.text,
            language=uc.language,
            tags=uc.tags,
            context=context,
            expected_spans=uc.expected_spans,
            forbidden_spans=uc.forbidden_spans,
            rule_edits=uc.rule_edits,
            notes=uc.notes,
        )
        saved = store.save(stored)
        date_root = self._date_dir(slug, day)
        if llm_verdict is not None:
            verdict_dir = date_root / "llm_verdicts"
            verdict_dir.mkdir(parents=True, exist_ok=True)
            payload = llm_verdict.to_dict() if hasattr(llm_verdict, "to_dict") else dict(llm_verdict)
            _atomic_write(
                verdict_dir / f"{saved.id}-v{saved.version}.json",
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        if recommendations is not None:
            rec_dir = date_root / "recommendations"
            rec_dir.mkdir(parents=True, exist_ok=True)
            payload = recommendations.to_dict() if hasattr(recommendations, "to_dict") else dict(recommendations)
            _atomic_write(
                rec_dir / f"{saved.id}-v{saved.version}.json",
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        self._write_date_index(slug, day)
        return saved

    def _write_date_index(self, slug: str, date: str) -> None:
        store = self.usecases(slug, date=date)
        rows = store.list(limit=1000)
        _atomic_write(
            self._date_dir(slug, date) / "index.json",
            json.dumps({"date": date, "usecases": rows}, indent=2, ensure_ascii=False) + "\n",
        )

    def export(self, slug: str, *, date: str | None = None) -> bytes:
        if self.get(slug) is None:
            raise SessionError(f"unknown session {slug!r}")
        day = date or _today()
        folder = self._date_dir(slug, day)
        if not folder.is_dir():
            raise SessionError(f"no session folder for {slug}/{day}")
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in folder.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(folder).as_posix())
            meta = self._meta_path(slug)
            if meta.is_file():
                zf.write(meta, "session.json")
        return buf.getvalue()

    def describe(self, slug: str) -> dict[str, Any]:
        meta = self.get(slug)
        if meta is None:
            raise SessionError(f"unknown session {slug!r}")
        dates = []
        for day in self.dates(slug):
            store = self.usecases(slug, date=day)
            dates.append({"date": day, "usecase_count": len(store.list(limit=1000))})
        out = meta.to_dict()
        out["dates"] = dates
        return out
