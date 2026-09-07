"""TrainingExample / TrainingDataset — versioned JSONL corpora with residency."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, Optional, Union

Residency = Literal["portable", "local"]

PathLike = Union[str, Path]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class TrainingExample:
    """One column-level training row (features + label + provenance)."""

    features: dict[str, Any]
    label: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    residency: Residency = "portable"
    samples: Optional[list[str]] = None
    text_context: Optional[str] = None
    contains_raw_values: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "features": dict(self.features),
            "label": dict(self.label),
            "provenance": dict(self.provenance),
            "residency": self.residency,
        }
        if self.contains_raw_values or self.residency == "local":
            out["contains_raw_values"] = bool(self.contains_raw_values)
        if self.samples is not None:
            out["samples"] = list(self.samples)
        if self.text_context is not None:
            out["text_context"] = self.text_context
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrainingExample":
        residency = str(data.get("residency") or "portable").lower()
        if residency not in ("portable", "local"):
            residency = "portable"
        return cls(
            features=dict(data.get("features") or {}),
            label=dict(data.get("label") or {}),
            provenance=dict(data.get("provenance") or {}),
            residency=residency,  # type: ignore[arg-type]
            samples=list(data["samples"]) if isinstance(data.get("samples"), list) else None,
            text_context=str(data["text_context"]) if data.get("text_context") is not None else None,
            contains_raw_values=bool(data.get("contains_raw_values")),
        )


@dataclass
class TrainingDataset:
    """Versioned collection of training examples with checksum + residency."""

    examples: list[TrainingExample] = field(default_factory=list)
    version: str = "1"
    residency: Residency = "portable"
    created_at: str = field(default_factory=_utc_now_iso)
    source: str = "training_export"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def checksum(self) -> str:
        """Stable SHA-256 over canonical JSONL body (examples only)."""
        body = "\n".join(
            json.dumps(ex.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            for ex in self.examples
        )
        return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_manifest(self) -> dict[str, Any]:
        return {
            "kind": "redibis.training_dataset",
            "version": self.version,
            "residency": self.residency,
            "created_at": self.created_at,
            "source": self.source,
            "row_count": len(self.examples),
            "checksum": self.checksum,
            "meta": dict(self.meta),
        }

    def iter_dicts(self) -> Iterator[dict[str, Any]]:
        for ex in self.examples:
            yield ex.to_dict()

    def write_jsonl(self, path: PathLike, *, write_manifest: bool = True) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for row in self.iter_dicts():
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if write_manifest:
            man = out.with_suffix(out.suffix + ".manifest.json")
            if out.suffix == ".jsonl":
                man = out.with_name(out.stem + ".manifest.json")
            man.write_text(
                json.dumps(self.to_manifest(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return out

    @classmethod
    def from_jsonl(cls, path: PathLike) -> "TrainingDataset":
        p = Path(path)
        examples: list[TrainingExample] = []
        residency: Residency = "portable"
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                ex = TrainingExample.from_dict(row)
                examples.append(ex)
                if ex.residency == "local" or ex.contains_raw_values:
                    residency = "local"
        meta: dict[str, Any] = {}
        version = "1"
        created_at = _utc_now_iso()
        source = "training_export"
        man = p.with_name(p.stem + ".manifest.json")
        if man.is_file():
            try:
                payload = json.loads(man.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    meta = dict(payload.get("meta") or {})
                    version = str(payload.get("version") or version)
                    created_at = str(payload.get("created_at") or created_at)
                    source = str(payload.get("source") or source)
                    r = str(payload.get("residency") or residency).lower()
                    if r in ("portable", "local"):
                        residency = r  # type: ignore[assignment]
            except (OSError, json.JSONDecodeError):
                pass
        return cls(
            examples=examples,
            version=version,
            residency=residency,
            created_at=created_at,
            source=source,
            meta=meta,
        )

    def stats(self) -> dict[str, Any]:
        n_pii = sum(1 for e in self.examples if e.label.get("is_pii") is True)
        n_not = sum(1 for e in self.examples if e.label.get("is_pii") is False)
        n_corr = sum(1 for e in self.examples if e.label.get("corrected_engine") is True)
        entities: dict[str, int] = {}
        sources: dict[str, int] = {}
        for e in self.examples:
            et = str(e.label.get("entity_type") or "") or "(none)"
            entities[et] = entities.get(et, 0) + 1
            src = str(e.label.get("source") or "") or "(none)"
            sources[src] = sources.get(src, 0) + 1
        return {
            "row_count": len(self.examples),
            "residency": self.residency,
            "checksum": self.checksum,
            "is_pii_true": n_pii,
            "is_pii_false": n_not,
            "corrected_engine": n_corr,
            "entity_types": dict(sorted(entities.items())),
            "label_sources": dict(sorted(sources.items())),
            "with_samples": sum(1 for e in self.examples if e.samples),
        }


def context_hash(*, table: str, column: str, parts: Iterable[Any]) -> str:
    """Dedupe / drift key — never includes raw cell values."""
    blob = "|".join(str(p) for p in (table, column, *parts))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def dump_example(ex: TrainingExample) -> dict[str, Any]:
    """Helper for tests — dataclass asdict with nested copies."""
    return asdict(ex)
