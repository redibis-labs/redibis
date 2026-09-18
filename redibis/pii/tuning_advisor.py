"""LLM tuning recommendations — advisory only. Never applied automatically."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Sequence

from redibis.pii.privacy import scrub_pii_text
from redibis.pii.rules.term_target import advise
from redibis.pii.scan.result import Detection, TextScanConfig
from redibis.pii.text_preprocess.expanders._util import tokenize_with_spans

logger = logging.getLogger("pii.tuning_advisor")

KIND = "redibis.tuning_recommendations"
SCHEMA_VERSION = "1.0"
TARGETS = (
    "noise_terms", "exclude_terms", "ner_stoplist", "context_cues",
    "quantity_units", "patterns", "min_score",
)


@dataclass
class Recommendation:
    target: str
    action: str
    value: Any
    entity_type: str = ""
    reason: str = ""
    evidence: tuple[tuple[int, int], ...] = ()
    confidence: float = 0.0
    validation: dict = field(default_factory=dict)
    support: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "action": self.action,
            "value": self.value,
            "entity_type": self.entity_type,
            "reason": self.reason,
            "evidence": [list(p) for p in self.evidence],
            "confidence": round(float(self.confidence), 4),
            "validation": dict(self.validation or {}),
            "support": int(self.support),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Recommendation":
        data = dict(raw or {})
        evidence = []
        for item in data.get("evidence") or ():
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                evidence.append((int(item[0]), int(item[1])))
        return cls(
            target=str(data.get("target") or ""),
            action=str(data.get("action") or "add"),
            value=data.get("value"),
            entity_type=str(data.get("entity_type") or ""),
            reason=str(data.get("reason") or ""),
            evidence=tuple(evidence),
            confidence=float(data.get("confidence") or 0.0),
            validation=dict(data.get("validation") or {}),
            support=int(data.get("support") or 1),
        )


@dataclass
class RecommendationSet:
    kind: str = KIND
    schema_version: str = SCHEMA_VERSION
    run_uuid: str = ""
    text_digest: str = ""
    model: str = ""
    provider: str = ""
    items: tuple[Recommendation, ...] = ()
    applied: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "run_uuid": self.run_uuid,
            "text_digest": self.text_digest,
            "model": self.model,
            "provider": self.provider,
            "items": [i.to_dict() for i in self.items],
            "applied": False if self.applied is False else bool(self.applied),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "RecommendationSet":
        data = dict(raw or {})
        items = tuple(
            Recommendation.from_dict(item)
            for item in (data.get("items") or data.get("recommendations") or ())
            if isinstance(item, Mapping)
        )
        return cls(
            kind=str(data.get("kind") or KIND),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            run_uuid=str(data.get("run_uuid") or ""),
            text_digest=str(data.get("text_digest") or ""),
            model=str(data.get("model") or ""),
            provider=str(data.get("provider") or ""),
            items=items,
            applied=False,
            error=str(data.get("error") or ""),
        )


_SYSTEM = """\
You recommend engine-tuning changes for a free-text PII scanner.
Return JSON only:
{"recommendations":[{"target":"noise_terms","value":"…","action":"add","reason":"…","evidence":[[s,e]],"confidence":0.8}]}
Targets: noise_terms, exclude_terms, ner_stoplist, context_cues, quantity_units, patterns, min_score.
Recommendations are advisory. Do not invent offsets. Reasons must not contain source digits.
"""


def _token_count(text: str) -> int:
    return max(1, len(tokenize_with_spans(text or "")))


def validate(
    recs: RecommendationSet,
    *,
    text: str,
    spans: Sequence[Any] = (),
    overlay: Any = None,
    accepted: Sequence[Any] = (),
) -> RecommendationSet:
    """Keep invalid recommendations and mark them. Never drop silently."""
    items: list[Recommendation] = []
    accepted_keys = {
        (int(getattr(s, "start", None) if not isinstance(s, Mapping) else s.get("start") or 0),
         int(getattr(s, "end", None) if not isinstance(s, Mapping) else s.get("end") or 0),
         str(getattr(s, "entity_type", "") if not isinstance(s, Mapping) else s.get("entity_type") or ""))
        for s in (accepted or ())
    }
    n_tokens = _token_count(text)
    for rec in recs.items:
        flags: dict[str, Any] = dict(rec.validation or {})
        flags.setdefault("ok", True)
        reason = scrub_pii_text(rec.reason) if rec.reason else ""
        evidence = []
        for start, end in rec.evidence:
            if start < 0 or end > len(text) or end <= start or not text[start:end]:
                flags["ok"] = False
                flags["bad_evidence"] = True
            else:
                evidence.append((start, end))
        if rec.target == "patterns":
            pattern = ""
            if isinstance(rec.value, Mapping):
                pattern = str(rec.value.get("pattern") or "")
            else:
                pattern = str(rec.value or "")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                flags["ok"] = False
                flags["invalid_regex"] = str(exc)
                compiled = None
            if compiled is not None and text:
                hits = list(compiled.finditer(text))
                flags["match_count"] = len(hits)
                if n_tokens and len(hits) / n_tokens > 0.4:
                    flags["overbroad"] = True
                    flags["ok"] = False
        elif rec.target in ("noise_terms", "exclude_terms", "ner_stoplist"):
            term = ""
            if isinstance(rec.value, Mapping):
                term = str(rec.value.get("value") or rec.value.get("term") or "")
            else:
                term = str(rec.value or "")
            advice = advise(term, text=text, spans=spans, overlay=overlay, requested=rec.target)
            matching = next((a for a in advice if a.target == rec.target), None)
            removed = matching.would_remove if matching else ()
            flags["would_remove"] = [list(r) for r in removed]
            if any(row in accepted_keys for row in removed):
                flags["would_remove_accepted"] = True
        elif rec.target == "min_score":
            try:
                score = float(rec.value)
            except (TypeError, ValueError):
                flags["ok"] = False
                flags["bounds"] = "not a number"
            else:
                if not 0.0 <= score <= 1.0:
                    flags["ok"] = False
                    flags["bounds"] = "min_score must be in [0, 1]"
        items.append(replace(
            rec,
            reason=reason,
            evidence=tuple(evidence) if evidence else rec.evidence,
            validation=flags,
        ))
    return replace(recs, items=tuple(items), applied=False)


def aggregate(sets: list[RecommendationSet]) -> RecommendationSet:
    """Merge by (target, value), sum support, keep top evidence."""
    buckets: dict[tuple[str, str], Recommendation] = {}
    for recset in sets or ():
        for rec in recset.items:
            key = (rec.target, json.dumps(rec.value, sort_keys=True, ensure_ascii=False, default=str))
            if key not in buckets:
                buckets[key] = replace(rec, support=max(1, rec.support))
            else:
                existing = buckets[key]
                evidence = tuple(list(existing.evidence) + [e for e in rec.evidence if e not in existing.evidence])[:8]
                buckets[key] = replace(
                    existing,
                    support=existing.support + max(1, rec.support),
                    confidence=max(existing.confidence, rec.confidence),
                    evidence=evidence,
                    reason=existing.reason or rec.reason,
                )
    ordered = sorted(buckets.values(), key=lambda r: (-r.support, r.target, str(r.value)))
    return RecommendationSet(items=tuple(ordered), applied=False)


def recommend(
    text: str,
    *,
    engine_result: Any = None,
    llm_verdict: Any = None,
    curation: Any = None,
    overlay: Any = None,
    config: Optional[TextScanConfig] = None,
    refiner: Any = None,
    run_uuid: str = "",
) -> RecommendationSet:
    """Call the tuning advisor. Never mutates ``overlay``."""
    from redibis.pii.run_store import input_digest

    cfg = config or TextScanConfig()
    t0 = time.perf_counter()
    digest = input_digest(text)
    overlay_payload = overlay.to_dict() if hasattr(overlay, "to_dict") else (overlay or {})
    engine_spans = []
    if engine_result is not None:
        if hasattr(engine_result, "to_dict"):
            engine_spans = list(engine_result.to_dict(return_text=True).get("spans") or [])
        elif isinstance(engine_result, Mapping):
            engine_spans = list(engine_result.get("spans") or engine_result.get("detections") or [])
    verdict_payload = llm_verdict.to_dict() if hasattr(llm_verdict, "to_dict") else (llm_verdict or {})
    curation_payload = curation.to_dict() if hasattr(curation, "to_dict") else (curation or {})

    empty = RecommendationSet(run_uuid=run_uuid, text_digest=digest, applied=False)
    if refiner is None:
        return replace(empty, error="no LLM refiner attached")

    user = json.dumps({
        "text": text,
        "engine_spans": engine_spans[:40],
        "llm_verdict": verdict_payload,
        "curation": curation_payload,
        "overlay": overlay_payload,
        "language": getattr(cfg, "language", "en"),
    }, ensure_ascii=False, default=str)
    model = ""
    provider = ""
    try:
        resolver = getattr(refiner, "resolve_with_path", None)
        if callable(resolver):
            prov, model_id, _path = resolver()
            model = str(model_id or "")
            provider = str(getattr(prov, "name", "") or getattr(prov, "provider", "") or "")
        raw = refiner._call_model(user, system=_SYSTEM, model_role="pii.tuning_advisor")
    except Exception as exc:
        logger.warning("tuning advisor failed: %s", exc)
        return replace(empty, model=model, provider=provider, error=str(exc))

    items: list[Recommendation] = []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        start, end = (raw or "").find("{"), (raw or "").rfind("}")
        try:
            data = json.loads((raw or "")[start:end + 1]) if start >= 0 and end > start else {}
        except json.JSONDecodeError:
            data = {}
    rows = []
    if isinstance(data, dict):
        rows = data.get("recommendations") or data.get("items") or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        items.append(Recommendation.from_dict(row))
    recset = RecommendationSet(
        run_uuid=run_uuid,
        text_digest=digest,
        model=model,
        provider=provider,
        items=tuple(items),
        applied=False,
    )
    accepted = []
    if isinstance(curation_payload, Mapping):
        for entry in curation_payload.get("entries") or ():
            if isinstance(entry, Mapping) and entry.get("decision") == "accept":
                key = entry.get("key") or {}
                accepted.append(key)
    _ = (time.perf_counter() - t0)
    return validate(recset, text=text, spans=engine_spans, overlay=overlay, accepted=accepted)
