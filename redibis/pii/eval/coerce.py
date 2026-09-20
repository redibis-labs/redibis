"""Turn Gateway scan JSON / use cases into a portable eval dataset.

The evaluation runner only scores ``redibis.text_span_eval_dataset``. Operators
download that kind from Text Gateway, but they also keep scan envelopes and
use-case files. Coerce those shapes so CLI ``pii eval --dataset DIR`` and the
Evaluations page can score the same folder.
"""

from __future__ import annotations

from typing import Any, Mapping

from redibis.pii.eval.span_metrics import DATASET_KIND, OFFSET_UNIT, SCHEMA_VERSION_1_2


def _slice(text: str, start: int, end: int) -> str:
    if 0 <= start <= end <= len(text):
        return text[start:end]
    return ""


def _labeled(span: Any) -> bool:
    return isinstance(span, Mapping) and bool(str(span.get("entity_type") or "").strip())


def _span_row(span: Mapping[str, Any], text: str, *, index: int, prefix: str = "s") -> dict[str, Any]:
    start = int(span.get("start") or 0)
    end = int(span.get("end") or 0)
    value = str(span.get("value") or span.get("text") or _slice(text, start, end))
    row: dict[str, Any] = {
        "id": str(span.get("id") or f"{prefix}{index}"),
        "start": start,
        "end": end,
        "entity_type": str(span.get("entity_type") or "").strip().upper(),
        "value": value,
    }
    reason = str(span.get("reason") or "")
    if reason:
        row["reason"] = reason
    return row


def _case(
    *,
    case_id: str,
    text: str,
    language: str,
    expected: list[dict[str, Any]],
    forbidden: list[dict[str, Any]] | None = None,
    name: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": case_id or "case-1",
        "text": text,
        "language": language or "en",
        "tags": list(tags or []),
        "expected_spans": expected,
        "forbidden_spans": list(forbidden or []),
    }
    if name:
        out["name"] = name
    return out


def _dataset(cases: list[dict[str, Any]], *, dataset_id: str = "") -> dict[str, Any]:
    return {
        "kind": DATASET_KIND,
        "schema_version": SCHEMA_VERSION_1_2,
        "offset_unit": OFFSET_UNIT,
        "id": dataset_id,
        "cases": cases,
    }


def from_gateway_scan(raw: Mapping[str, Any], *, default_id: str = "scan") -> dict[str, Any] | None:
    """Scan envelope → one-case dataset. Needs the original ``text``."""
    text = str(raw.get("text") or raw.get("source_text") or "")
    if not text:
        return None
    pii = ((raw.get("analysers") or {}) if isinstance(raw.get("analysers"), Mapping) else {}).get("pii") or {}
    if not isinstance(pii, Mapping):
        pii = {}
    spans = list(pii.get("spans") or [])
    rejected = list(pii.get("rejected_spans") or [])
    lang = str((raw.get("text_meta") or {}).get("language") or raw.get("language") or "en")
    run = str(raw.get("run_uuid") or default_id or "scan")[:48] or "scan"
    expected = [_span_row(s, text, index=i + 1) for i, s in enumerate(spans) if _labeled(s)]
    forbidden = [
        _span_row(s, text, index=i + 1, prefix="f")
        for i, s in enumerate(rejected)
        if _labeled(s)
    ]
    return _dataset(
        [_case(case_id=run, text=text, language=lang, expected=expected, forbidden=forbidden, name=run)],
        dataset_id=run,
    )


def from_usecase(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    text = str(raw.get("text") or "")
    if not text:
        return None
    cid = str(raw.get("id") or raw.get("name") or "usecase")[:48] or "usecase"
    expected = [
        _span_row(s, text, index=i + 1)
        for i, s in enumerate(raw.get("expected_spans") or [])
        if _labeled(s)
    ]
    forbidden = [
        _span_row(s, text, index=i + 1, prefix="f")
        for i, s in enumerate(raw.get("forbidden_spans") or [])
        if _labeled(s)
    ]
    return _dataset(
        [_case(
            case_id=cid,
            text=text,
            language=str(raw.get("language") or "en"),
            expected=expected,
            forbidden=forbidden,
            name=str(raw.get("name") or ""),
            tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
        )],
        dataset_id=cid,
    )


def coerce_eval_dataset(raw: Any, *, default_id: str = "imported") -> dict[str, Any] | None:
    """Return a dataset mapping, or None when ``raw`` is not a known eval shape."""
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if kind == DATASET_KIND and isinstance(raw.get("cases"), list):
        return dict(raw)
    if kind in ("redibis.text_span_eval_report", "redibis.text_span_eval_batch_report"):
        if kind == "redibis.text_span_eval_batch_report":
            cases = []
            for entry in raw.get("files") or []:
                if isinstance(entry, Mapping):
                    cases.extend(list(entry.get("cases") or []))
        else:
            cases = list(raw.get("cases") or [])
        usable = []
        for i, case in enumerate(cases):
            if not isinstance(case, Mapping):
                continue
            text = str(case.get("text") or "")
            if not text:
                continue
            expected = list(case.get("expected_spans") or [])
            usable.append(_case(
                case_id=str(case.get("id") or f"case-{i + 1}"),
                text=text,
                language=str(case.get("language") or "en"),
            expected=[_span_row(s, text, index=j + 1) for j, s in enumerate(expected) if _labeled(s)],
            forbidden=[
                    _span_row(s, text, index=j + 1, prefix="f")
                    for j, s in enumerate(case.get("forbidden_spans") or [])
                    if _labeled(s)
                ],
                name=str(case.get("name") or ""),
                tags=[str(t) for t in (case.get("tags") or []) if str(t).strip()],
            ))
        if not usable:
            return None
        return _dataset(usable, dataset_id=str(raw.get("dataset_id") or default_id))
    if kind == "redibis.text_usecase" or (
        "text" in raw and ("expected_spans" in raw or "forbidden_spans" in raw) and "analysers" not in raw
    ):
        return from_usecase(raw)
    if isinstance(raw.get("analysers"), Mapping) and "pii" in (raw.get("analysers") or {}):
        return from_gateway_scan(raw, default_id=default_id)
    return None
