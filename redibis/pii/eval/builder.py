"""Authoring corpora (values, no offsets) → portable span datasets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from redibis.pii.eval.span_metrics import (
    DATASET_KIND,
    OFFSET_UNIT,
    SCHEMA_VERSION_1_2,
    current_redibis_version,
)

CORPUS_KIND = "redibis.text_span_eval_corpus"


class CorpusBuildError(ValueError):
    """Raised when a corpus value cannot be located in the case text."""


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        return yaml.safe_load(text)
    return json.loads(text)


def _find_value(text: str, value: str, *, occurrence: int = 1, after: str = "") -> tuple[int, int]:
    if not value:
        raise CorpusBuildError("value is empty")
    start_from = 0
    if after:
        anchor = text.find(after)
        if anchor < 0:
            raise CorpusBuildError(f"anchor {after!r} not found")
        start_from = anchor + len(after)
    found = 0
    pos = start_from
    while True:
        idx = text.find(value, pos)
        if idx < 0:
            raise CorpusBuildError(f"value {value!r} not found verbatim in text")
        found += 1
        if found == occurrence:
            return idx, idx + len(value)
        pos = idx + 1


def _span_from_author(case_id: str, text: str, raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    entity = str(raw.get("entity_type") or "").strip().upper()
    if not entity:
        raise CorpusBuildError(f"{case_id}: expected[{index}] missing entity_type")
    if "start" in raw and "end" in raw:
        start, end = int(raw["start"]), int(raw["end"])
    else:
        value = str(raw.get("value") or raw.get("text") or "")
        if not value:
            raise CorpusBuildError(f"{case_id}: expected[{index}] has no value")
        try:
            start, end = _find_value(
                text,
                value,
                occurrence=int(raw.get("occurrence") or 1),
                after=str(raw.get("after") or ""),
            )
        except CorpusBuildError as exc:
            raise CorpusBuildError(f"{case_id}: {exc} (value={value!r})") from exc
        if text[start:end] != value:
            raise CorpusBuildError(f"{case_id}: slice mismatch for {value!r}")
    span: dict[str, Any] = {
        "id": str(raw.get("id") or f"s{index + 1}"),
        "start": start,
        "end": end,
        "entity_type": entity,
        "grade": str(raw.get("grade") or "strict"),
    }
    if raw.get("canonical") not in (None, ""):
        span["canonical"] = str(raw["canonical"])
    if raw.get("spoken"):
        span["spoken"] = True
    if raw.get("note"):
        span["note"] = str(raw["note"])
    if raw.get("defect"):
        span["defect"] = str(raw["defect"])
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    if tags:
        span["tags"] = tags
    return span


def build_dataset(corpus: Mapping[str, Any], *, source: str = "") -> dict[str, Any]:
    kind = corpus.get("kind")
    if kind not in {CORPUS_KIND, DATASET_KIND, None}:
        raise CorpusBuildError(f"unsupported corpus kind {kind!r}")
    cases_raw = corpus.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise CorpusBuildError("cases must be a non-empty array")
    cases: list[dict[str, Any]] = []
    for raw in cases_raw:
        if not isinstance(raw, Mapping):
            raise CorpusBuildError("case must be an object")
        case_id = str(raw.get("id") or "").strip()
        if not case_id:
            raise CorpusBuildError("case id is required")
        text = raw.get("text")
        if not isinstance(text, str):
            raise CorpusBuildError(f"{case_id}: text must be a string")
        expected_raw = raw.get("expected") or raw.get("expected_spans") or []
        if not isinstance(expected_raw, list):
            raise CorpusBuildError(f"{case_id}: expected must be an array")
        expected = [
            _span_from_author(case_id, text, span, i)
            for i, span in enumerate(expected_raw)
            if isinstance(span, Mapping)
        ]
        forbidden_raw = raw.get("forbidden") or raw.get("forbidden_spans") or []
        forbidden = []
        if isinstance(forbidden_raw, list):
            for i, span in enumerate(forbidden_raw):
                if not isinstance(span, Mapping):
                    continue
                try:
                    item = _span_from_author(case_id, text, span, i)
                except CorpusBuildError as exc:
                    raise CorpusBuildError(f"{case_id} forbidden: {exc}") from exc
                item["reason"] = str(span.get("reason") or "")
                forbidden.append(item)
        tags = [str(t) for t in (raw.get("tags") or []) if str(t).strip()]
        cases.append({
            "id": case_id,
            "text": text,
            "language": str(raw.get("language") or "en"),
            "tags": tags,
            "expected_spans": expected,
            "forbidden_spans": forbidden,
        })
    return {
        "kind": DATASET_KIND,
        "schema_version": SCHEMA_VERSION_1_2,
        "redibis_version": current_redibis_version(),
        "offset_unit": OFFSET_UNIT,
        "id": str(corpus.get("id") or ""),
        "normalization_profile": str(corpus.get("normalization_profile") or "v1"),
        "source": source,
        "cases": cases,
    }


def discover_corpus_files(path: Path, *, recursive: bool = True) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    pattern = "**/*" if recursive else "*"
    out: list[Path] = []
    for p in sorted(path.glob(pattern)):
        if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"} and not p.is_symlink():
            out.append(p)
    return out


def build_path(corpus_dir: Path, out_dir: Path, *, recursive: bool = True) -> list[Path]:
    """Convert each corpus file to a sibling JSON dataset. Returns written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for src in discover_corpus_files(corpus_dir, recursive=recursive):
        raw = _load(src)
        if not isinstance(raw, Mapping):
            raise CorpusBuildError(f"{src}: corpus must be a mapping")
        if raw.get("kind") not in {CORPUS_KIND, None} and raw.get("kind") == DATASET_KIND:
            continue
        if raw.get("kind") not in {CORPUS_KIND, None} and "cases" not in raw:
            continue
        dataset = build_dataset(raw, source=src.as_posix())
        dest = out_dir / (src.stem + ".json")
        dest.write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(dest)
    if not written:
        raise CorpusBuildError(f"no corpora found under {corpus_dir}")
    return written
