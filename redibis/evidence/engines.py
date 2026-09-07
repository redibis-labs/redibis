"""Project detector output into plugin-neutral ``EngineEvidenceRecord`` maps.

Works on ``PIIDetection`` or any object/dict with the same field names. This
module stays a leaf: it does not import ``redibis.pii``.

Plugin contract
---------------
Future engines persist under ``engine_evidence[<id>]`` with ``engine_id``,
``ran``, ``reason`` (when skipped), ``score`` / ``hits`` / ``rates``, plus
optional ``version``, ``duration_ms``, ``error``, and ``extra``. Extra fields
unknown to this schema are preserved on ``EngineEvidenceRecord.extra``.

``ran=False`` is authoritative: leftover scores or hits must not be treated as
evidence that the engine executed, and they are not replayed into equations.

Plugin-only scores do **not** vote in built-in equation modes
(``strict`` / ``balanced`` / ``lenient`` / ``independent``). They are stored
for investigation and shown on ``contributing_engines()``; verdicts still come
from the first-party engines those equations already know.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from redibis.evidence.models import EngineEvidenceRecord


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _engine_state(obj: Any, key: str) -> dict:
    states = _get(obj, "engine_states") or {}
    if not isinstance(states, dict):
        return {}
    block = states.get(key)
    return block if isinstance(block, dict) else {}


def _ran_from_state(state: dict, *, fallback: bool) -> bool:
    if "ran" in state:
        return bool(state.get("ran"))
    return fallback


def _record(
    engine_id: str,
    *,
    name: str,
    kind: str,
    ran: bool,
    reason: str = "",
    score: Any = None,
    entity: Any = None,
    label: Any = None,
    match_rate: Any = None,
    hits: Optional[list] = None,
    rates: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> EngineEvidenceRecord:
    return EngineEvidenceRecord(
        engine_id=engine_id,
        name=name,
        kind=kind,
        ran=bool(ran),
        reason="" if ran else (reason or "did not run"),
        score=float(score) if score is not None else None,
        entity=str(entity) if entity is not None else None,
        label=str(label) if label is not None else None,
        match_rate=float(match_rate) if match_rate is not None else None,
        hits=list(hits or []),
        rates=dict(rates or {}),
        extra=dict(extra or {}),
    )


def project_engine_evidence(detection: Any) -> dict[str, dict[str, Any]]:
    """Return ``{engine_id: EngineEvidenceRecord.to_dict()}`` including skips."""
    records = list_engine_records(detection)
    return {r.engine_id: r.to_dict() for r in records}


def list_engine_records(detection: Any) -> list[EngineEvidenceRecord]:
    regex_hits = list(_get(detection, "regex_hits") or [])
    ner_hits = list(_get(detection, "ner_hits") or [])
    regex_state = _engine_state(detection, "regex")
    ner_state = _engine_state(detection, "ner")
    phone_state = _engine_state(detection, "phone")
    nid_state = _engine_state(detection, "nid")
    imei_state = _engine_state(detection, "imei")
    imsi_state = _engine_state(detection, "imsi")
    geo_state = _engine_state(detection, "geo")

    presidio_score = _get(detection, "presidio_score")
    presidio_ran = _ran_from_state(
        regex_state,
        fallback=presidio_score is not None or bool(regex_hits),
    )
    gliner_score = _get(detection, "gliner_score")
    gliner_ran = _ran_from_state(
        ner_state,
        fallback=gliner_score is not None or bool(ner_hits),
    )
    phone_score = _get(detection, "phone_score")
    phone_rates = {
        k: v for k, v in {
            "valid_rate": _get(detection, "phone_valid_rate"),
            "mobile_rate": _get(detection, "phone_mobile_rate"),
            "msisdn_valid_rate": _get(detection, "msisdn_valid_rate"),
            "regions": _get(detection, "phone_regions"),
        }.items() if v is not None
    }
    phone_ran = _ran_from_state(
        phone_state,
        fallback=phone_score is not None or bool(phone_rates),
    )

    nid_rate = _get(detection, "nid_valid_rate")
    nid_ran = _ran_from_state(nid_state, fallback=nid_rate is not None)
    imei_rate = _get(detection, "imei_valid_rate")
    imei_ran = _ran_from_state(imei_state, fallback=imei_rate is not None)
    imsi_rate = _get(detection, "imsi_valid_rate")
    imsi_ran = _ran_from_state(imsi_state, fallback=imsi_rate is not None)
    geo_rate = _get(detection, "geo_confidence")
    geo_ran = _ran_from_state(geo_state, fallback=geo_rate is not None)

    llm_score = _get(detection, "llm_score")
    llm_ran = any(
        v is not None
        for v in (llm_score, _get(detection, "llm_verdict"), _get(detection, "llm_reasoning"))
    )
    learned_score = _get(detection, "learned_score")
    learned_ran = learned_score is not None

    records = [
        _record(
            "presidio",
            name="Microsoft Presidio",
            kind="ner+regex",
            ran=presidio_ran,
            reason=str(regex_state.get("reason") or "pii.engines excludes regex"),
            score=presidio_score if presidio_ran else None,
            entity=(_get(detection, "presidio_pattern") or _get(detection, "entity_type"))
            if presidio_ran else None,
            match_rate=_get(detection, "presidio_match_rate") if presidio_ran else None,
            hits=regex_hits if presidio_ran else [],
        ),
        _record(
            "regex_catalog",
            name="Redibis regex catalog",
            kind="regex",
            ran=bool(regex_hits) or presidio_ran,
            reason="no regex hits" if not regex_hits else "",
            hits=regex_hits if (regex_hits or presidio_ran) else [],
            extra={
                "entities": {
                    hit.get("entity_type"): hit.get("score")
                    for hit in regex_hits
                    if isinstance(hit, dict) and hit.get("entity_type")
                }
            } if regex_hits else {},
        ),
        _record(
            "gliner",
            name="GLiNER",
            kind="ner",
            ran=gliner_ran,
            reason=str(ner_state.get("reason") or "pii.engines excludes ner"),
            score=gliner_score if gliner_ran else None,
            label=_get(detection, "gliner_label") if gliner_ran else None,
            match_rate=_get(detection, "gliner_match_rate") if gliner_ran else None,
            hits=ner_hits if gliner_ran else [],
            extra={"ner_engine": _get(detection, "ner_engine")} if _get(detection, "ner_engine") else {},
        ),
        _record(
            "phone",
            name="libphonenumber",
            kind="validator",
            ran=phone_ran,
            reason=str(phone_state.get("reason") or "no phone-shaped column"),
            score=phone_score if phone_ran else None,
            entity=_get(detection, "phone_entity"),
            rates=phone_rates,
            extra=dict(phone_state) if phone_state else {},
        ),
        _record(
            "nid",
            name="Egyptian National ID",
            kind="validator",
            ran=nid_ran,
            reason=str(nid_state.get("reason") or "nid validator did not run"),
            score=nid_rate if nid_ran else None,
            rates={
                k: v for k, v in {
                    "valid_rate": nid_rate,
                    "checked": _get(detection, "nid_checked"),
                    "age_p05": _get(detection, "nid_age_p05"),
                    "age_p95": _get(detection, "nid_age_p95"),
                    "gov_distinct": _get(detection, "nid_gov_distinct"),
                    "monotonic_rate": _get(detection, "nid_monotonic_rate"),
                    "unique_rate": _get(detection, "nid_unique_rate"),
                }.items() if v is not None
            },
        ),
        _record(
            "imei",
            name="IMEI Luhn validator",
            kind="validator",
            ran=imei_ran,
            reason=str(imei_state.get("reason") or "imei validator did not run"),
            score=imei_rate if imei_ran else None,
            rates={
                k: v for k, v in {
                    "valid_rate": imei_rate,
                    "checked": _get(detection, "imei_checked"),
                }.items() if v is not None
            },
        ),
        _record(
            "imsi",
            name="IMSI validator",
            kind="validator",
            ran=imsi_ran,
            reason=str(imsi_state.get("reason") or "imsi validator did not run"),
            score=imsi_rate if imsi_ran else None,
            rates={
                k: v for k, v in {
                    "valid_rate": imsi_rate,
                    "checked": _get(detection, "imsi_checked"),
                }.items() if v is not None
            },
        ),
        _record(
            "geo",
            name="Geo pair validator",
            kind="validator",
            ran=geo_ran,
            reason=str(geo_state.get("reason") or "geo pair not corroborated"),
            score=geo_rate if geo_ran else None,
            rates={"confidence": geo_rate} if geo_rate is not None else {},
        ),
        _record(
            "llm_refiner",
            name="LiteLLM refiner",
            kind="llm",
            ran=llm_ran,
            reason="not triggered",
            score=llm_score if llm_ran else None,
            extra={
                k: v for k, v in {
                    "verdict": _get(detection, "llm_verdict"),
                    "reasoning": _get(detection, "llm_reasoning"),
                }.items() if v is not None
            },
        ),
        _record(
            "learned",
            name="Structured learned classifier",
            kind="classifier",
            ran=learned_ran,
            reason="pii.learned.enabled=false",
            score=learned_score if learned_ran else None,
            label=_get(detection, "learned_label"),
            entity=_get(detection, "learned_entity"),
            extra={"engine": _get(detection, "learned_engine")} if _get(detection, "learned_engine") else {},
        ),
    ]

    existing = _get(detection, "engine_evidence") or {}
    if isinstance(existing, dict):
        known = {r.engine_id for r in records}
        for engine_id, block in existing.items():
            if engine_id in known or not isinstance(block, dict):
                continue
            payload = dict(block)
            payload.setdefault("engine_id", engine_id)
            records.append(EngineEvidenceRecord.from_dict(payload))

    states = _get(detection, "engine_states") or {}
    if isinstance(states, dict):
        known = {r.engine_id for r in records}
        alias = {"regex": "presidio", "ner": "gliner"}
        for key, block in states.items():
            engine_id = alias.get(key, key)
            if engine_id in known or not isinstance(block, dict):
                continue
            records.append(_record(
                str(engine_id),
                name=str(block.get("name") or engine_id),
                kind=str(block.get("kind") or "plugin"),
                ran=bool(block.get("ran", True)),
                reason=str(block.get("reason") or ""),
                score=block.get("score"),
                extra={k: v for k, v in block.items() if k not in {"ran", "reason", "score", "name", "kind"}},
            ))
    return records


def merge_compat_blocks(records: Iterable[EngineEvidenceRecord]) -> dict[str, dict[str, Any]]:
    """Legacy ``pii_evidence`` keyed blocks plus generic ``engine_evidence``."""
    by_id = {r.engine_id: r for r in records}
    compat: dict[str, dict[str, Any]] = {}
    for engine_id, rec in by_id.items():
        block: dict[str, Any] = {"ran": rec.ran}
        if rec.reason and not rec.ran:
            block["reason"] = rec.reason
        if rec.score is not None:
            block["score"] = rec.score
        if rec.entity is not None:
            block["entity" if engine_id != "gliner" else "label"] = rec.entity
        if rec.label is not None and engine_id == "gliner":
            block["label"] = rec.label
        if rec.match_rate is not None:
            block["match_rate"] = rec.match_rate
        if engine_id == "presidio" and rec.hits:
            block["pattern_hits"] = rec.hits
        if engine_id == "regex_catalog" and rec.extra.get("entities"):
            block["entities"] = rec.extra["entities"]
        if engine_id == "phone":
            block.update({
                k: rec.rates[k] for k in (
                    "valid_rate", "mobile_rate", "msisdn_valid_rate", "regions",
                ) if k in rec.rates
            })
        if engine_id == "llm_refiner":
            if rec.extra.get("verdict") is not None:
                block["verdict"] = rec.extra["verdict"]
            if rec.extra.get("reasoning") is not None:
                block["reasoning"] = rec.extra["reasoning"]
        if engine_id == "learned":
            if rec.label is not None:
                block["label"] = rec.label
            if rec.entity is not None:
                block["entity_type"] = rec.entity
            if rec.extra.get("engine"):
                block["engine"] = rec.extra["engine"]
        compat[engine_id] = block
    compat["engine_evidence"] = {r.engine_id: r.to_dict() for r in by_id.values()}
    return compat
