"""Assemble N files into an LLM context envelope (PLAN §5.5).

Refuses raw-PII evidence unless ``--allow-raw-pii --reason`` is given.
``--max-bytes`` drops whole files (never truncates). Globs are expanded and
de-duplicated before inclusion.
"""

from __future__ import annotations

import glob
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

KIND_BY_SUFFIX = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".csv": "csv",
    ".tsv": "tsv",
    ".py": "python",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
    ".sql": "sql",
    ".jsonl": "jsonl",
}

CONTEXT_KIND = "redibis.llm_context"
CONTEXT_SCHEMA = "1.0"


class ContextBuildError(Exception):
    """Context assembly refused or failed."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def expand_add_paths(patterns: Iterable[str]) -> list[Path]:
    """Glob-expand ``--add`` patterns, de-duplicating after resolve."""
    seen: set[str] = set()
    out: list[Path] = []
    for pat in patterns:
        text = str(pat).strip()
        if not text:
            continue
        matches = glob.glob(text, recursive=True)
        if not matches:
            p = Path(text).expanduser()
            if p.exists():
                matches = [str(p)]
            else:
                raise ContextBuildError(f"no files matched --add {text!r}")
        for m in matches:
            path = Path(m).expanduser()
            if path.is_dir():
                continue
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(path.resolve())
    return out


def _detect_kind(path: Path, parsed: Any, is_json: bool) -> str:
    if is_json and isinstance(parsed, dict):
        kind = parsed.get("kind")
        if isinstance(kind, str) and kind:
            return kind
        return "json"
    return KIND_BY_SUFFIX.get(path.suffix.lower(), "text")


def _contains_raw_pii(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    sensitivity = parsed.get("sensitivity")
    if isinstance(sensitivity, dict) and sensitivity.get("contains_raw_pii") is True:
        return True
    return _has_literal_sample_payload(parsed)


def _has_literal_sample_payload(obj: Any) -> bool:
    """True when JSON still carries sample/top-value literals, regardless of flags."""
    if isinstance(obj, dict):
        samples = obj.get("samples")
        if isinstance(samples, dict) and samples.get("values"):
            return True
        if obj.get("top_values"):
            return True
        if obj.get("example_values") or obj.get("sample"):
            return True
        return any(_has_literal_sample_payload(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_literal_sample_payload(v) for v in obj)
    return False


def _table_from_parsed(parsed: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(parsed, dict):
        return None, None
    table = parsed.get("table")
    if isinstance(table, dict):
        return (
            str(table.get("name") or "") or None,
            str(table.get("run_id") or "") or None,
        )
    if isinstance(table, str) and table:
        return table, None
    return None, None


def build_context(
    paths: list[Path],
    *,
    max_bytes: int = 400_000,
    allow_raw_pii: bool = False,
    reason: str | None = None,
) -> dict:
    """Build the §5.5 context envelope from resolved file paths."""
    if allow_raw_pii and not (reason or "").strip():
        raise ContextBuildError("--allow-raw-pii requires --reason TEXT")
    if (reason or "").strip() and not allow_raw_pii:
        raise ContextBuildError("--reason is only valid together with --allow-raw-pii")

    sources: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    used = 0
    raw_pii_seen = False

    for path in paths:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ContextBuildError(f"cannot stat {path}: {exc}") from exc

        if used + size > max_bytes:
            dropped.append({
                "path": str(path),
                "reason": "max_bytes",
                "bytes": size,
            })
            continue

        raw = path.read_bytes()
        text: Optional[str] = None
        parsed: Any = None
        is_json = path.suffix.lower() == ".json"
        if is_json:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                is_json = False
        if not is_json:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")

        if _contains_raw_pii(parsed):
            raw_pii_seen = True
            if not allow_raw_pii:
                raise ContextBuildError(
                    f"{path} contains raw PII (sensitivity.contains_raw_pii=true); "
                    "pass --allow-raw-pii --reason TEXT to include it"
                )

        kind = _detect_kind(path, parsed, is_json)
        table_name, run_id = _table_from_parsed(parsed)
        sensitivity = None
        if isinstance(parsed, dict) and isinstance(parsed.get("sensitivity"), dict):
            sensitivity = parsed["sensitivity"]

        idx = len(sources)
        sources.append({
            "index": idx,
            "path": str(path),
            "kind": kind,
            "bytes": size,
            "table": table_name,
            "run_id": run_id,
            "sensitivity": sensitivity,
        })
        if is_json:
            items.append({"source": idx, "content": parsed})
        else:
            items.append({"source": idx, "kind": kind, "text": text or ""})
        used += size

    return {
        "kind": CONTEXT_KIND,
        "schema_version": CONTEXT_SCHEMA,
        "created_at": _utc_now_iso(),
        "max_bytes": max_bytes,
        "bytes": used,
        "allow_raw_pii": bool(allow_raw_pii),
        "raw_pii_reason": (reason or "").strip() or None,
        "raw_pii_included": bool(allow_raw_pii and raw_pii_seen),
        "sources": sources,
        "dropped": dropped,
        "items": items,
    }
