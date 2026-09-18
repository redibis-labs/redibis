"""Operational run registry for text-gateway / eval / de-id sessions.

Stores input **digests**, never the input text. Digests are HMAC-SHA256 with a
per-install secret so short identifiers are not rainbow-table invertible from
the files. Provenance records are content-addressed and kept indefinitely;
runs expire via TTL and are deleted by the reaper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Union

from redibis.pii.eval.provenance import new_run_uuid
from redibis.pii.provenance import ScanProvenance, _identity_payload

PathLike = Union[str, Path]

_STORE: Optional["ProvenanceRunStore"] = None

# Below this Unicode length the digest is omitted: MSISDN / NID / PAN are
# enumerable even under HMAC if the attacker also has the install key.
MIN_DIGEST_CHARS = 32
HMAC_PREFIX = "hmac-sha256:"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339(value: str) -> Optional[float]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def default_run_dir() -> Path:
    env = os.environ.get("REDIBIS_PII_RUN_DIR")
    if env:
        return Path(env).expanduser()
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "pii_runs"


def _ttl_seconds() -> Optional[float]:
    raw = os.environ.get("REDIBIS_PII_RUN_TTL_SECONDS") or ""
    if raw.strip():
        try:
            return float(raw)
        except ValueError:
            return None
    days = os.environ.get("REDIBIS_PII_RUN_TTL_DAYS") or ""
    if days.strip():
        try:
            return float(days) * 86400.0
        except ValueError:
            return None
    return None


def _load_hmac_key(root: Path) -> bytes:
    env = (os.environ.get("REDIBIS_PII_RUN_HMAC_KEY") or "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    key_path = root / ".hmac_key"
    if key_path.is_file():
        text = key_path.read_text(encoding="utf-8").strip()
        try:
            raw = bytes.fromhex(text)
            if raw:
                return raw
        except ValueError:
            return hashlib.sha256(text.encode("utf-8")).digest()
    key = os.urandom(32)
    root.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key.hex() + "\n", encoding="utf-8")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def input_digest(
    text: str | bytes | None,
    *,
    key: bytes | None = None,
    min_chars: int = MIN_DIGEST_CHARS,
) -> str:
    """HMAC-SHA256 of the input, or empty for short / missing bodies.

    Correlation ("same input as that run?") holds within an install. The
    digest is not a rainbow-table of the raw PII without the install key,
    and inputs shorter than ``min_chars`` are not digested at all.
    """
    if text is None:
        return ""
    if isinstance(text, bytes):
        blob = text
        char_count = len(text.decode("utf-8", errors="replace"))
    else:
        blob = text.encode("utf-8")
        char_count = len(text)
    if char_count < min_chars:
        return ""
    secret = key if key is not None else _load_hmac_key(default_run_dir())
    return HMAC_PREFIX + hmac.new(secret, blob, hashlib.sha256).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class RunRecord:
    run_uuid: str
    kind: str
    provenance_uuid: str
    started: str
    finished: str
    actor: str
    tenant: str
    label: str
    input_digest: str
    char_count: int
    case_count: int = 0
    outcome: dict = field(default_factory=dict)
    parent_run_uuid: str = ""
    provenance_degraded: bool = False
    provenance_degraded_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_uuid": self.run_uuid,
            "kind": self.kind,
            "provenance_uuid": self.provenance_uuid,
            "started": self.started,
            "finished": self.finished,
            "actor": self.actor,
            "tenant": self.tenant,
            "label": self.label,
            "input_digest": self.input_digest,
            "char_count": self.char_count,
            "case_count": self.case_count,
            "outcome": dict(self.outcome or {}),
            "parent_run_uuid": self.parent_run_uuid,
            "provenance_degraded": self.provenance_degraded,
            "provenance_degraded_reason": self.provenance_degraded_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RunRecord":
        raw = dict(data or {})
        return cls(
            run_uuid=str(raw.get("run_uuid") or ""),
            kind=str(raw.get("kind") or ""),
            provenance_uuid=str(raw.get("provenance_uuid") or ""),
            started=str(raw.get("started") or ""),
            finished=str(raw.get("finished") or ""),
            actor=str(raw.get("actor") or ""),
            tenant=str(raw.get("tenant") or ""),
            label=str(raw.get("label") or ""),
            input_digest=str(raw.get("input_digest") or ""),
            char_count=int(raw.get("char_count") or 0),
            case_count=int(raw.get("case_count") or 0),
            outcome=dict(raw.get("outcome") or {}),
            parent_run_uuid=str(raw.get("parent_run_uuid") or ""),
            provenance_degraded=bool(raw.get("provenance_degraded")),
            provenance_degraded_reason=str(raw.get("provenance_degraded_reason") or ""),
        )


class ProvenanceRunStore:
    """Filesystem store: provenance JSON (immutable) + run records (TTL)."""

    def __init__(self, root: PathLike | None = None):
        self.root = Path(root or default_run_dir()).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "provenance").mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()
        self._hmac_key = _load_hmac_key(self.root)

    def digest(self, text: str | bytes | None) -> str:
        return input_digest(text, key=self._hmac_key)

    def _prov_path(self, uid: str) -> Path:
        safe = (uid or "").strip()
        if not safe or "/" in safe or "\\" in safe:
            raise ValueError(f"invalid provenance uuid: {uid!r}")
        return self.root / "provenance" / f"{safe}.json"

    def _run_path(self, uid: str) -> Path:
        safe = (uid or "").strip()
        if not safe or "/" in safe or "\\" in safe:
            raise ValueError(f"invalid run uuid: {uid!r}")
        return self.root / "runs" / f"{safe}.json"

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
            return {"by_provenance": {}, "by_pack": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"run index is corrupt at {path}; refusing to wipe history: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"run index is corrupt at {path}: not a mapping")
        return {
            "by_provenance": dict(raw.get("by_provenance") or {}),
            "by_pack": dict(raw.get("by_pack") or {}),
        }

    def _save_index(self, index: dict[str, Any]) -> None:
        _atomic_write(
            self._index_path(),
            json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )

    def put_provenance(self, rec: ScanProvenance | Mapping[str, Any]) -> ScanProvenance:
        parsed = rec if isinstance(rec, ScanProvenance) else ScanProvenance.from_dict(rec)
        uid = parsed.provenance_uuid
        if not uid:
            return parsed
        path = self._prov_path(uid)
        payload = parsed.to_dict()
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict):
                old = ScanProvenance.from_dict(existing)
                if _identity_payload(old) != _identity_payload(parsed):
                    raise RuntimeError(
                        f"provenance {uid} already stored with a different identity payload"
                    )
            return parsed
        _atomic_write(
            path,
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        return parsed

    def get_provenance(self, uid: str) -> Optional[ScanProvenance]:
        if not (uid or "").strip():
            return None
        try:
            path = self._prov_path(uid)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return ScanProvenance.from_dict(raw)

    def put_run(self, rec: RunRecord) -> RunRecord:
        if not rec.run_uuid:
            raise ValueError("run_uuid is required")
        self.reap_expired()
        path = self._run_path(rec.run_uuid)
        payload = rec.to_dict()
        payload.pop("text", None)
        payload.pop("input", None)
        payload.pop("body", None)
        _atomic_write(
            path,
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        with self._index_lock():
            index = self._load_index()
            by_prov = dict(index.get("by_provenance") or {})
            if rec.provenance_uuid:
                runs = list(by_prov.get(rec.provenance_uuid) or [])
                if rec.run_uuid not in runs:
                    runs.append(rec.run_uuid)
                by_prov[rec.provenance_uuid] = runs
            by_pack = dict(index.get("by_pack") or {})
            prov = self.get_provenance(rec.provenance_uuid) if rec.provenance_uuid else None
            if prov is not None:
                for layer in prov.pack_layers:
                    pack_uid = str((layer or {}).get("uuid") or "")
                    if not pack_uid:
                        continue
                    runs = list(by_pack.get(pack_uid) or [])
                    if rec.run_uuid not in runs:
                        runs.append(rec.run_uuid)
                    by_pack[pack_uid] = runs
            self._save_index({"by_provenance": by_prov, "by_pack": by_pack})
        return rec

    def get_run(self, uid: str) -> Optional[RunRecord]:
        if not (uid or "").strip():
            return None
        try:
            path = self._run_path(uid)
        except ValueError:
            return None
        if not path.is_file():
            return None
        rec = self._read_run_file(path)
        if rec is None:
            return None
        if self._record_expired(rec, path):
            return None
        return rec

    def _read_run_file(self, path: Path) -> Optional[RunRecord]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return RunRecord.from_dict(raw)

    def _record_expired(self, rec: RunRecord, path: Path) -> bool:
        ttl = _ttl_seconds()
        if ttl is None:
            return False
        stamp = _parse_rfc3339(rec.finished) or _parse_rfc3339(rec.started)
        if stamp is None:
            try:
                stamp = path.stat().st_mtime
            except OSError:
                return False
        return (time.time() - stamp) > ttl

    def reap_expired(self) -> int:
        """Delete run files past TTL. Provenance records are never reaped."""
        ttl = _ttl_seconds()
        if ttl is None:
            return 0
        removed: list[str] = []
        for path in list((self.root / "runs").glob("*.json")):
            rec = self._read_run_file(path)
            if rec is None:
                continue
            if not self._record_expired(rec, path):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(rec.run_uuid)
        if not removed:
            return 0
        drop = set(removed)
        with self._index_lock():
            try:
                index = self._load_index()
            except RuntimeError:
                return len(removed)
            by_prov = {
                k: [u for u in (v or []) if u not in drop]
                for k, v in (index.get("by_provenance") or {}).items()
            }
            by_pack = {
                k: [u for u in (v or []) if u not in drop]
                for k, v in (index.get("by_pack") or {}).items()
            }
            self._save_index({"by_provenance": by_prov, "by_pack": by_pack})
        return len(removed)

    def list_runs(
        self,
        *,
        provenance_uuid: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[RunRecord]:
        self.reap_expired()
        rows: list[RunRecord] = []
        wanted_ids: Optional[list[str]] = None
        if provenance_uuid:
            index = self._load_index()
            wanted_ids = list((index.get("by_provenance") or {}).get(provenance_uuid) or [])
        if wanted_ids is not None:
            for uid in reversed(wanted_ids):
                rec = self.get_run(uid)
                if rec is None:
                    continue
                if kind and rec.kind != kind:
                    continue
                rows.append(rec)
                if len(rows) >= limit:
                    break
            return rows
        files = sorted(
            (self.root / "runs").glob("*.json"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for path in files:
            rec = self.get_run(path.stem)
            if rec is None:
                continue
            if kind and rec.kind != kind:
                continue
            rows.append(rec)
            if len(rows) >= limit:
                break
        return rows

    def runs_for_pack_uuid(self, pack_uuid: str, *, limit: int = 100) -> list[RunRecord]:
        uid = (pack_uuid or "").strip()
        if not uid:
            return []
        self.reap_expired()
        index = self._load_index()
        ids = list((index.get("by_pack") or {}).get(uid) or [])
        rows: list[RunRecord] = []
        for run_uid in reversed(ids):
            rec = self.get_run(run_uid)
            if rec is None:
                continue
            rows.append(rec)
            if len(rows) >= limit:
                break
        return rows


def get_run_store() -> ProvenanceRunStore:
    global _STORE
    if _STORE is None:
        _STORE = ProvenanceRunStore()
    return _STORE


def reset_run_store_cache() -> None:
    global _STORE
    _STORE = None


def record_run(
    *,
    kind: str,
    provenance: ScanProvenance | Mapping[str, Any] | None = None,
    text: str | None = None,
    char_count: int = 0,
    case_count: int = 0,
    outcome: Mapping[str, Any] | None = None,
    actor: str = "",
    tenant: str = "",
    label: str = "",
    run_uuid: str | None = None,
    parent_run_uuid: str = "",
    store: ProvenanceRunStore | None = None,
    started: str | None = None,
) -> RunRecord:
    """Persist a run. ``text`` is digested and discarded."""
    backend = store or get_run_store()
    parsed: Optional[ScanProvenance] = None
    if isinstance(provenance, ScanProvenance):
        parsed = provenance
        backend.put_provenance(parsed)
    elif isinstance(provenance, Mapping):
        parsed = ScanProvenance.from_dict(provenance)
        if parsed.provenance_uuid:
            backend.put_provenance(parsed)
    digest = backend.digest(text) if text is not None else ""
    if text is not None and not char_count:
        char_count = len(text)
    now = _utc_now()
    rec = RunRecord(
        run_uuid=new_run_uuid(run_uuid),
        kind=str(kind or ""),
        provenance_uuid=parsed.provenance_uuid if parsed is not None else "",
        started=started or now,
        finished=now,
        actor=str(actor or ""),
        tenant=str(tenant or ""),
        label=str(label or ""),
        input_digest=digest,
        char_count=int(char_count or 0),
        case_count=int(case_count or 0),
        outcome=dict(outcome or {}),
        parent_run_uuid=str(parent_run_uuid or ""),
        provenance_degraded=bool(parsed.provenance_degraded) if parsed is not None else False,
        provenance_degraded_reason=(
            parsed.provenance_degraded_reason if parsed is not None else ""
        ),
    )
    return backend.put_run(rec)


__all__ = [
    "HMAC_PREFIX",
    "MIN_DIGEST_CHARS",
    "ProvenanceRunStore",
    "RunRecord",
    "default_run_dir",
    "get_run_store",
    "input_digest",
    "record_run",
    "reset_run_store_cache",
]
