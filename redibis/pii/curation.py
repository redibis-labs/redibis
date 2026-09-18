"""Specialist decisions over detected / proposed spans.

Never scans. Never mutates a DetectionResult; overlaid at render / export time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from redibis.pii.scan.result import Detection

DECISIONS = ("accept", "reject", "modify")
SOURCES = ("engine", "llm_verdict", "manual")
KIND = "redibis.span_curation"
SCHEMA_VERSION = "1.0"

# Surfaces must never appear in the persisted record (offsets + types only).
_SURFACE_KEYS = frozenset({"text", "surface", "value", "slice"})


class CurationError(ValueError):
    """Raised when a modify would leave the original text."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub(reason: str) -> str:
    if not reason:
        return ""
    from redibis.pii.privacy import scrub_pii_text

    return scrub_pii_text(str(reason))[:400]


@dataclass(frozen=True)
class SpanKey:
    start: int
    end: int
    entity_type: str
    source: str = "engine"

    def __post_init__(self) -> None:
        src = (self.source or "engine").strip() or "engine"
        if src not in SOURCES:
            object.__setattr__(self, "source", "engine")
        et = str(self.entity_type or "").strip().upper()
        object.__setattr__(self, "entity_type", et)

    def as_tuple(self) -> tuple[int, int, str, str]:
        return (int(self.start), int(self.end), self.entity_type, self.source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": int(self.start),
            "end": int(self.end),
            "entity_type": self.entity_type,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "SpanKey":
        data = dict(raw or {})
        return cls(
            start=int(data.get("start") or 0),
            end=int(data.get("end") or 0),
            entity_type=str(data.get("entity_type") or ""),
            source=str(data.get("source") or "engine") or "engine",
        )

    @classmethod
    def from_span(cls, span: Any, *, source: str = "engine") -> "SpanKey":
        if isinstance(span, Mapping):
            return cls(
                start=int(span.get("start") or 0),
                end=int(span.get("end") or 0),
                entity_type=str(span.get("entity_type") or ""),
                source=str(span.get("source") or source) or source,
            )
        return cls(
            start=int(getattr(span, "start", 0) or 0),
            end=int(getattr(span, "end", 0) or 0),
            entity_type=str(getattr(span, "entity_type", "") or ""),
            source=str(getattr(span, "source", "") or source) or source,
        )


@dataclass(frozen=True)
class CurationEntry:
    key: SpanKey
    decision: str
    new_start: int | None = None
    new_end: int | None = None
    new_entity_type: str = ""
    reason: str = ""
    by: str = ""
    at: str = ""
    trimmed: bool = False

    def __post_init__(self) -> None:
        dec = (self.decision or "").strip().lower()
        if dec not in DECISIONS:
            raise CurationError(f"unknown decision {self.decision!r}")
        object.__setattr__(self, "decision", dec)
        if self.new_entity_type:
            object.__setattr__(self, "new_entity_type", str(self.new_entity_type).strip().upper())
        if self.reason:
            object.__setattr__(self, "reason", _scrub(self.reason))
        if not self.at:
            object.__setattr__(self, "at", _utc_now())

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key.to_dict(),
            "decision": self.decision,
        }
        if self.new_start is not None:
            out["new_start"] = int(self.new_start)
        if self.new_end is not None:
            out["new_end"] = int(self.new_end)
        if self.new_entity_type:
            out["new_entity_type"] = self.new_entity_type
        if self.reason:
            out["reason"] = self.reason
        if self.by:
            out["by"] = self.by
        if self.at:
            out["at"] = self.at
        if self.trimmed:
            out["trimmed"] = True
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "CurationEntry":
        data = dict(raw or {})
        key_raw = data.get("key") if isinstance(data.get("key"), Mapping) else data
        return cls(
            key=SpanKey.from_dict(key_raw),
            decision=str(data.get("decision") or "accept"),
            new_start=None if data.get("new_start") is None else int(data["new_start"]),
            new_end=None if data.get("new_end") is None else int(data["new_end"]),
            new_entity_type=str(data.get("new_entity_type") or ""),
            reason=str(data.get("reason") or ""),
            by=str(data.get("by") or ""),
            at=str(data.get("at") or ""),
            trimmed=bool(data.get("trimmed")),
        )


@dataclass(frozen=True)
class Curation:
    run_uuid: str = ""
    text_digest: str = ""
    entries: tuple[CurationEntry, ...] = ()
    auto_trim: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "schema_version": SCHEMA_VERSION,
            "run_uuid": self.run_uuid,
            "text_digest": self.text_digest,
            "entries": [e.to_dict() for e in self.entries],
            "auto_trim": bool(self.auto_trim),
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Curation":
        data = dict(raw or {})
        entries = tuple(
            CurationEntry.from_dict(item)
            for item in (data.get("entries") or ())
            if isinstance(item, Mapping)
        )
        return cls(
            run_uuid=str(data.get("run_uuid") or ""),
            text_digest=str(data.get("text_digest") or ""),
            entries=entries,
            auto_trim=bool(data.get("auto_trim")),
        )

    def with_entry(self, entry: CurationEntry) -> "Curation":
        """Replace any existing entry for the same key, or append."""
        kept = [e for e in self.entries if e.key.as_tuple() != entry.key.as_tuple()]
        kept.append(entry)
        return replace(self, entries=tuple(kept))

    def without_key(self, key: SpanKey) -> "Curation":
        return replace(
            self,
            entries=tuple(e for e in self.entries if e.key.as_tuple() != key.as_tuple()),
        )


def assert_no_surfaces(payload: Mapping[str, Any]) -> None:
    """Raise if a persisted curation record contains span surfaces."""
    blob = json.dumps(dict(payload), ensure_ascii=False)
    # Keys named text/surface/value are the leak; reasons are scrubbed separately.
    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                key = str(k)
                if key in _SURFACE_KEYS and path != "kind":
                    raise CurationError(f"curation record must not contain {key!r} at {path}")
                _walk(v, f"{path}.{key}" if path else key)
        elif isinstance(node, (list, tuple)):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")

    _walk(payload, "")
    _ = blob


def _as_detection(span: Any, *, text: str, source: str = "engine") -> Detection:
    if isinstance(span, Detection):
        src = str(span.source or source)
        if src != span.source:
            return replace(span, source=src)
        return span
    data = dict(span) if isinstance(span, Mapping) else {}
    start = int(data.get("start") if data else getattr(span, "start", 0) or 0)
    end = int(data.get("end") if data else getattr(span, "end", 0) or 0)
    et = str((data.get("entity_type") if data else getattr(span, "entity_type", "")) or "")
    slice_text = text[start:end] if 0 <= start <= end <= len(text) else str(
        data.get("text") if data else getattr(span, "text", "") or ""
    )
    return Detection(
        entity_type=et,
        score=float((data.get("score") if data else getattr(span, "score", 0.0)) or 0.0),
        engine=str((data.get("engine") if data else getattr(span, "engine", "")) or source or "manual"),
        start=start,
        end=end,
        text=slice_text,
        detected=True,
        recognizer=str((data.get("recognizer") if data else getattr(span, "recognizer", "")) or ""),
        validator=str((data.get("validator") if data else getattr(span, "validator", "")) or ""),
        is_proposal=bool(data.get("is_proposal") if data else getattr(span, "is_proposal", False)),
        canonical=str((data.get("canonical") if data else getattr(span, "canonical", "")) or ""),
        source=str(data.get("source") or source),
    )


def _span_identity(det: Detection) -> tuple[int, int, str, str]:
    src = str(getattr(det, "source", "") or "engine") or "engine"
    return (int(det.start or 0), int(det.end or 0), str(det.entity_type or ""), src)


def apply_curation(
    detections: Iterable[Any],
    curation: Curation | Mapping[str, Any] | None,
    *,
    text: str,
) -> list[Detection]:
    """Curated view: drop rejects, rewrite modifies, append manual accepts.

    Every returned Detection satisfies ``text[start:end] == d.text``. A modify
    whose slice is empty or whose bounds leave the text is refused — not
    clamped silently.
    """
    rec = curation if isinstance(curation, Curation) else Curation.from_dict(curation)
    source_dets = [_as_detection(d, text=text) for d in (detections or ())]
    by_key: dict[tuple[int, int, str, str], CurationEntry] = {
        e.key.as_tuple(): e for e in rec.entries
    }
    out: list[Detection] = []
    seen: set[tuple[int, int, str]] = set()

    for det in source_dets:
        identity = _span_identity(det)
        entry = by_key.get(identity)
        if entry is None:
            # Also match engine spans that the operator keyed without source.
            entry = by_key.get((identity[0], identity[1], identity[2], "engine"))
        if entry is None:
            if 0 <= int(det.start or 0) <= int(det.end or 0) <= len(text):
                slice_text = text[int(det.start or 0): int(det.end or 0)]
                if det.text != slice_text:
                    det = replace(det, text=slice_text)
            out.append(det)
            seen.add((int(det.start or 0), int(det.end or 0), str(det.entity_type or "")))
            continue
        if entry.decision == "reject":
            continue
        if entry.decision == "accept":
            out.append(det)
            seen.add((int(det.start or 0), int(det.end or 0), str(det.entity_type or "")))
            continue
        # modify
        new_start = int(entry.new_start) if entry.new_start is not None else int(det.start or 0)
        new_end = int(entry.new_end) if entry.new_end is not None else int(det.end or 0)
        new_type = entry.new_entity_type or det.entity_type
        if new_start < 0 or new_end > len(text) or new_end <= new_start:
            raise CurationError(
                f"modify [{new_start}:{new_end}] is empty or out of range for text of {len(text)}"
            )
        slice_text = text[new_start:new_end]
        if not slice_text:
            raise CurationError(f"modify [{new_start}:{new_end}] is an empty slice")
        out.append(replace(
            det,
            start=new_start,
            end=new_end,
            text=slice_text,
            entity_type=new_type,
            source=entry.key.source,
        ))
        seen.add((new_start, new_end, str(new_type or "")))

    for entry in rec.entries:
        if entry.decision != "accept":
            continue
        if entry.key.source != "manual" and entry.key.source != "llm_verdict":
            continue
        start, end, et = int(entry.key.start), int(entry.key.end), entry.key.entity_type
        if entry.new_start is not None:
            start = int(entry.new_start)
        if entry.new_end is not None:
            end = int(entry.new_end)
        if entry.new_entity_type:
            et = entry.new_entity_type
        ident = (start, end, et)
        if ident in seen:
            continue
        if start < 0 or end > len(text) or end <= start:
            raise CurationError(
                f"accept [{start}:{end}] is empty or out of range for text of {len(text)}"
            )
        slice_text = text[start:end]
        if not slice_text:
            raise CurationError(f"accept [{start}:{end}] is an empty slice")
        out.append(Detection(
            entity_type=et,
            score=1.0,
            engine="manual" if entry.key.source == "manual" else "llm",
            start=start,
            end=end,
            text=slice_text,
            recognizer=entry.key.source,
            source=entry.key.source,
        ))
        seen.add(ident)

    for det in out:
        start, end = int(det.start or 0), int(det.end or 0)
        if text[start:end] != det.text:
            raise CurationError(
                f"slice integrity failed: text[{start}:{end}]={text[start:end]!r} != {det.text!r}"
            )
    return out


def entity_counts(detections: Sequence[Detection]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for det in detections:
        et = str(det.entity_type or "")
        counts[et] = counts.get(et, 0) + 1
    return counts
