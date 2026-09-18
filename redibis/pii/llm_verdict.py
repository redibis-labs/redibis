"""Independent LLM verdict — text + entity vocabulary only. Not an input to the engine."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from redibis.pii.privacy import scrub_pii_text
from redibis.pii.scan.result import Detection, TextScanConfig

logger = logging.getLogger("pii.llm_verdict")

KIND = "redibis.llm_verdict"
SCHEMA_VERSION = "1.0"

ENTITY_VOCABULARY = (
    "PERSON", "PHONE_NUMBER", "EG_NATIONAL_ID", "EMAIL_ADDRESS",
    "IMEI", "IMSI", "ICCID", "CREDIT_CARD", "LOCATION", "AGE",
    "PASSWORD_HASH", "API_KEY", "SECRET", "OTP", "PASSPORT",
    "IBAN_CODE", "SIM_PUK", "VOUCHER", "SUPPORT_TICKET",
)

_SYSTEM_VERDICT_EN = """\
You are a PII span detector. Given only the text and the entity vocabulary,
propose PII spans as JSON only:
{"spans":[{"start":0,"end":5,"entity_type":"PERSON","confidence":0.8,"reason":"short"}],
 "not_pii":[{"start":10,"end":14,"reason":"quantity"}]}
Offsets are Unicode code-point indices into the given text (Python string indices).
The span text MUST equal text[start:end] exactly. Do not invent offsets.
Do not assume any other detector has already run. You are not shown any engine hits.
Entity types: """ + ", ".join(ENTITY_VOCABULARY) + """
Do not flag role labels (Agent, Caller) or quantity+unit phrases.
When an address cue is present, span the whole address clause, not one keyword.
Only propose clear PII. Reasons must not contain source digits.
"""

_SYSTEM_VERDICT_AR = """\
You are a PII span detector for Arabic and mixed Arabic/English text, including
Egyptian call-center transcripts. Propose PII spans as JSON only:
{"spans":[{"start":0,"end":5,"entity_type":"PHONE_NUMBER","confidence":0.8,"reason":"short"}],
 "not_pii":[{"start":10,"end":14,"reason":"quantity"}]}
Offsets MUST be Unicode code-point indices into the given text (Python len/slice).
The span text MUST equal text[start:end] exactly. Do not invent offsets.
Do not assume any other detector has already run. You are not shown any engine hits.
Entity types: """ + ", ".join(ENTITY_VOCABULARY) + """
Do not flag role labels (Agent, Caller) or quantity+unit phrases.
When an address cue such as العنوان is present, span the whole address clause.
Only propose clear PII. Reasons must not contain source digits.
"""


@dataclass(frozen=True)
class LlmVerdictSpan:
    start: int
    end: int
    entity_type: str
    confidence: float
    text: str
    reason: str = ""

    def to_dict(self, *, return_text: bool = True) -> dict[str, Any]:
        return {
            "start": int(self.start),
            "end": int(self.end),
            "entity_type": self.entity_type,
            "confidence": round(float(self.confidence), 4),
            "text": self.text if return_text else "",
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LlmVerdict:
    kind: str = KIND
    schema_version: str = SCHEMA_VERSION
    run_uuid: str = ""
    text_digest: str = ""
    model: str = ""
    provider: str = ""
    prompt_sha256: str = ""
    mode: str = "independent"
    windows_scanned: int = 0
    windows_total: int = 0
    coverage_fraction: float = 1.0
    spans: tuple[LlmVerdictSpan, ...] = ()
    not_pii: tuple[dict, ...] = ()
    raw_response_sha256: str = ""
    latency_ms: float = 0.0
    error: str = ""

    def to_dict(self, *, return_text: bool = True) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "run_uuid": self.run_uuid,
            "text_digest": self.text_digest,
            "model": self.model,
            "provider": self.provider,
            "prompt_sha256": self.prompt_sha256,
            "mode": self.mode,
            "windows_scanned": int(self.windows_scanned),
            "windows_total": int(self.windows_total),
            "coverage_fraction": round(float(self.coverage_fraction), 4),
            "spans": [s.to_dict(return_text=return_text) for s in self.spans],
            "not_pii": [dict(row) for row in self.not_pii],
            "raw_response_sha256": self.raw_response_sha256,
            "latency_ms": round(float(self.latency_ms), 2),
            "error": self.error,
        }


def _system(config: TextScanConfig) -> str:
    arabic = bool(getattr(config, "arabic", False) or str(getattr(config, "language", "") or "").startswith("ar"))
    return _SYSTEM_VERDICT_AR if arabic else _SYSTEM_VERDICT_EN


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _parse_verdict_json(raw: str) -> tuple[list[dict], list[dict]]:
    text = (raw or "").strip()
    if not text:
        return [], []
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return [], []
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return [], []
    if not isinstance(data, dict):
        return [], []
    spans = data.get("spans") if isinstance(data.get("spans"), list) else []
    not_pii = data.get("not_pii") if isinstance(data.get("not_pii"), list) else []
    return [s for s in spans if isinstance(s, dict)], [s for s in not_pii if isinstance(s, dict)]


def run_llm_verdict(
    text: str,
    *,
    config: Optional[TextScanConfig] = None,
    refiner: Any = None,
    run_uuid: str = "",
) -> LlmVerdict:
    """Independent call: text + entity list. Errors are recorded, never swallowed."""
    cfg = config or TextScanConfig()
    t0 = time.perf_counter()
    from redibis.pii.run_store import input_digest

    digest = input_digest(text)
    if refiner is None:
        return LlmVerdict(
            run_uuid=run_uuid,
            text_digest=digest,
            error="no LLM refiner attached",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
    from redibis.pii.ner_window import windows as split_windows

    target = int(getattr(cfg, "llm_window_chars", 3500) or 3500)
    overlap = int(getattr(cfg, "llm_window_overlap", 300) or 300)
    max_wins = int(getattr(cfg, "llm_max_windows", 8) or 8)
    wins = split_windows(text or "", target_chars=target, overlap_chars=overlap)
    total = len(wins)
    if len(wins) > max_wins:
        wins = wins[:max_wins]
    system = _system(cfg)
    prompt_parts: list[str] = []
    raw_parts: list[str] = []
    spans_out: list[LlmVerdictSpan] = []
    not_pii_out: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    covered_end = 0
    model = ""
    provider = ""
    error = ""
    try:
        resolver = getattr(refiner, "resolve_with_path", None)
        if callable(resolver):
            prov, model_id, _path = resolver()
            model = str(model_id or "")
            provider = str(getattr(prov, "name", "") or getattr(prov, "provider", "") or "")
    except Exception as exc:
        error = str(exc)
        return LlmVerdict(
            run_uuid=run_uuid,
            text_digest=digest,
            model=model,
            provider=provider,
            prompt_sha256=_sha(system),
            error=error,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    try:
        for win in wins:
            covered_end = max(covered_end, win.end)
            prompt = (
                f"Text:\n{win.text}\n\n"
                "Offsets are 0-based indices into the Text block above, which is an excerpt.\n"
                "The first character of the Text block is offset 0.\n"
                "You are not shown any engine candidates.\n"
                'Reply as JSON: {"spans":[...],"not_pii":[...]}.'
            )
            prompt_parts.append(prompt)
            raw = refiner._call_model(prompt, system=system)
            raw_parts.append(raw or "")
            items, negatives = _parse_verdict_json(raw)
            for item in items:
                try:
                    start = int(item["start"]) + win.start
                    end = int(item["end"]) + win.start
                    et = str(item.get("entity_type") or "").upper().replace(" ", "_")
                    conf = float(item.get("confidence") or item.get("score") or 0.5)
                except (KeyError, TypeError, ValueError):
                    continue
                if not et or start < 0 or end > len(text) or start >= end:
                    continue
                slice_text = text[start:end]
                if not slice_text.strip():
                    continue
                if cfg.entities and et not in cfg.entities:
                    continue
                key = (start, end, et)
                if key in seen:
                    continue
                seen.add(key)
                spans_out.append(LlmVerdictSpan(
                    start=start,
                    end=end,
                    entity_type=et,
                    confidence=min(1.0, max(0.0, conf)),
                    text=slice_text,
                    reason=scrub_pii_text(str(item.get("reason") or ""))[:200],
                ))
            for row in negatives:
                try:
                    start = int(row["start"]) + win.start
                    end = int(row["end"]) + win.start
                except (KeyError, TypeError, ValueError):
                    continue
                if start < 0 or end > len(text) or start >= end:
                    continue
                not_pii_out.append({
                    "start": start,
                    "end": end,
                    "text": text[start:end],
                    "reason": scrub_pii_text(str(row.get("reason") or ""))[:200],
                })
    except Exception as exc:
        logger.warning("independent LLM verdict failed: %s", exc)
        error = str(exc)

    fraction = (covered_end / len(text)) if text else 1.0
    return LlmVerdict(
        run_uuid=run_uuid,
        text_digest=digest,
        model=model,
        provider=provider,
        prompt_sha256=_sha(system + "\n" + "\n".join(prompt_parts)),
        mode="independent",
        windows_scanned=len(wins),
        windows_total=total,
        coverage_fraction=round(fraction, 4),
        spans=tuple(spans_out),
        not_pii=tuple(not_pii_out),
        raw_response_sha256=_sha("\n".join(raw_parts)),
        latency_ms=(time.perf_counter() - t0) * 1000,
        error=error,
    )


def _span_key(span: Any) -> tuple[int, int, str]:
    if isinstance(span, Mapping):
        return (int(span.get("start") or 0), int(span.get("end") or 0), str(span.get("entity_type") or ""))
    return (
        int(getattr(span, "start", 0) or 0),
        int(getattr(span, "end", 0) or 0),
        str(getattr(span, "entity_type", "") or ""),
    )


def diff_against_engine(verdict: LlmVerdict | Mapping[str, Any], detections: Sequence[Any]) -> dict[str, Any]:
    """agree / llm_only / engine_only / type_conflict / boundary_conflict."""
    if isinstance(verdict, LlmVerdict):
        v_spans = list(verdict.spans)
    else:
        v_spans = list((verdict or {}).get("spans") or [])
    engine = list(detections or ())
    engine_keys = [_span_key(d) for d in engine]
    llm_keys = [_span_key(s) for s in v_spans]

    def _overlap(a: tuple[int, int, str], b: tuple[int, int, str]) -> int:
        return max(0, min(a[1], b[1]) - max(a[0], b[0]))

    agree: list[dict] = []
    type_conflict: list[dict] = []
    boundary_conflict: list[dict] = []
    matched_e: set[int] = set()
    matched_l: set[int] = set()
    for li, lk in enumerate(llm_keys):
        best = -1
        best_ov = 0
        for ei, ek in enumerate(engine_keys):
            if ei in matched_e:
                continue
            ov = _overlap(lk, ek)
            if ov > best_ov:
                best_ov, best = ov, ei
        if best < 0 or best_ov <= 0:
            continue
        ek = engine_keys[best]
        row = {"llm": list(lk), "engine": list(ek)}
        if lk == ek:
            agree.append(row)
            matched_e.add(best)
            matched_l.add(li)
        elif lk[0] == ek[0] and lk[1] == ek[1] and lk[2] != ek[2]:
            type_conflict.append(row)
            matched_e.add(best)
            matched_l.add(li)
        elif lk[2] == ek[2]:
            boundary_conflict.append(row)
            matched_e.add(best)
            matched_l.add(li)

    llm_only = [{"llm": list(llm_keys[i])} for i in range(len(llm_keys)) if i not in matched_l]
    engine_only = [{"engine": list(engine_keys[i])} for i in range(len(engine_keys)) if i not in matched_e]
    return {
        "agree": len(agree),
        "llm_only": len(llm_only),
        "engine_only": len(engine_only),
        "type_conflict": len(type_conflict),
        "boundary_conflict": len(boundary_conflict),
        "rows": {
            "agree": agree,
            "llm_only": llm_only,
            "engine_only": engine_only,
            "type_conflict": type_conflict,
            "boundary_conflict": boundary_conflict,
        },
    }


# Prompt builders are exported so tests can prove independence without a model.
def independent_prompt(window_text: str) -> str:
    return (
        f"Text:\n{window_text}\n\n"
        "Offsets are 0-based indices into the Text block above, which is an excerpt.\n"
        "The first character of the Text block is offset 0.\n"
        "You are not shown any engine candidates.\n"
        'Reply as JSON: {"spans":[...],"not_pii":[...]}.'
    )


def independent_system(language: str = "en") -> str:
    return _SYSTEM_VERDICT_AR if (language or "").startswith("ar") else _SYSTEM_VERDICT_EN
