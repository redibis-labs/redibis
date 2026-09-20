"""Append-only generations ledger — every engine/human opinion per column field.

Storage layout (contracts bucket)::

    _meta/generations/{table}/{column}.json

Never rewritten: a new scan appends a generation. Per ``(field, source)`` the
ledger is capped at the N most recent entries so files stay bounded.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from redibis.store.storage_backend import StorageBackend

GENERATIONS = (
    "profile",
    "regex",
    "ner",
    "phone",
    "custom_rule",
    "llm",
    "llm_synthesis",
    "supplied",
    "human",
)
FIELDS = (
    "pii",
    "entity_type",
    "classification",
    "definition",
    "tags",
    "logical_type",
    "quality_rules",
    "masking",
)
GENERATION_SET = frozenset(GENERATIONS)
FIELD_SET = frozenset(FIELDS)
CAP_PER_FIELD_SOURCE = 20

#: Steward-facing labels for the latest verdict from each producer.
SOURCE_LABELS = {
    "profile": "Profiler",
    "regex": "Regex",
    "ner": "NER",
    "phone": "Phone number",
    "custom_rule": "Business rules",
    "llm": "LLM enrich",
    "llm_synthesis": "Deep enrich (synthesis)",
    "supplied": "Supplied verdict",
    "human": "Data steward",
}

#: Order the steward sees PII engine cards. Missing engines still appear as "no verdict".
PII_ENGINE_ORDER = (
    "regex",
    "ner",
    "phone",
    "custom_rule",
    "llm",
    "llm_synthesis",
    "supplied",
    "human",
)
DEFINITION_SOURCE_ORDER = ("llm", "llm_synthesis", "human", "supplied")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generation_id(source: str, field: str, run_id: str, ts: str) -> str:
    raw = f"{source}|{field}|{run_id}|{ts}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Generation:
    field: str
    source: str
    value: Any
    confidence: float | None
    run_id: str
    ts: str
    detail: dict = field(default_factory=dict)
    fingerprint_key: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(
                self, "id", generation_id(self.source, self.field, self.run_id, self.ts),
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Generation":
        conf = d.get("confidence")
        try:
            conf_f = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf_f = None
        ts = str(d.get("ts") or _utc_now_iso())
        source = str(d.get("source") or "")
        field_name = str(d.get("field") or "")
        run_id = str(d.get("run_id") or "")
        gid = str(d.get("id") or "") or generation_id(source, field_name, run_id, ts)
        return cls(
            field=field_name,
            source=source,
            value=d.get("value"),
            confidence=conf_f,
            run_id=run_id,
            ts=ts,
            detail=dict(d.get("detail") or {}),
            fingerprint_key=str(d.get("fingerprint_key") or ""),
            id=gid,
        )


class GenerationLedger:
    """Append-only per-column generation history."""

    PREFIX = "_meta/generations"

    def __init__(self, backend: StorageBackend, bucket: str, *, cap: int = CAP_PER_FIELD_SOURCE):
        self.backend = backend
        self.bucket = bucket
        self.cap = int(cap)

    def _key(self, table: str, column: str) -> str:
        return f"{self.PREFIX}/{table}/{column}.json"

    def _load(self, table: str, column: str) -> list[dict]:
        key = self._key(table, column)
        if not self.backend.exists(self.bucket, key):
            return []
        raw = self.backend.get_json(self.bucket, key) or {}
        items = raw.get("generations") if isinstance(raw, dict) else raw
        return list(items or [])

    def _save(self, table: str, column: str, generations: list[dict]) -> None:
        self.backend.put_json(
            self.bucket,
            self._key(table, column),
            {"table": table, "column": column, "generations": generations},
        )

    def append(self, table: str, column: str, gens: Sequence[Generation | dict]) -> list[Generation]:
        """Append generations; never deletes earlier ones except the per-(field, source) cap.

        Text-bearing detail keys are scrubbed here so a producer that skips
        ``generations_from_detection`` cannot store raw reasoning.
        """
        existing = self._load(table, column)
        incoming: list[dict] = []
        for g in gens:
            if isinstance(g, Generation):
                obj = g
            elif isinstance(g, dict):
                obj = Generation.from_dict(g)
            else:
                continue
            incoming.append(_scrub_generation(obj).to_dict())
        combined = existing + incoming
        capped = _cap_most_recent(combined, self.cap)
        self._save(table, column, capped)
        return [Generation.from_dict(d) for d in incoming]

    def get(
        self,
        table: str,
        column: str,
        *,
        field: str | None = None,
    ) -> list[Generation]:
        items = [Generation.from_dict(d) for d in self._load(table, column)]
        if field:
            items = [g for g in items if g.field == field]
        return items

    def latest_by_source(self, table: str, column: str, field: str) -> dict[str, Generation]:
        out: dict[str, Generation] = {}
        for g in self.get(table, column, field=field):
            out[g.source] = g
        return out

    def get_by_id(self, table: str, column: str, gid: str) -> Optional[Generation]:
        for g in self.get(table, column):
            if g.id == gid:
                return g
        return None

    def list_columns(self, table: str) -> list[str]:
        prefix = f"{self.PREFIX}/{table}/"
        keys = self.backend.list_keys(self.bucket, prefix=prefix)
        cols: list[str] = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            name = key[len(prefix):]
            if "/" in name:
                continue
            cols.append(name[: -len(".json")])
        return sorted(cols)


def _cap_most_recent(items: list[dict], cap: int) -> list[dict]:
    """Keep chronological order; drop oldest beyond ``cap`` per (field, source)."""
    buckets: dict[tuple[str, str], list[int]] = {}
    for i, item in enumerate(items):
        key = (str(item.get("field") or ""), str(item.get("source") or ""))
        buckets.setdefault(key, []).append(i)
    drop: set[int] = set()
    for idxs in buckets.values():
        if len(idxs) > cap:
            drop.update(idxs[:-cap])
    return [item for i, item in enumerate(items) if i not in drop]


_SCRUB_KEYS = ("reasoning", "llm_reasoning", "rationale_text", "note", "reason")


def _scrub(text: Optional[str]) -> str:
    if not text:
        return ""
    from redibis.contracts.privacy import scrub_pii_text
    return scrub_pii_text(str(text))


def _scrub_detail(detail: dict) -> dict:
    d = dict(detail or {})
    for k in _SCRUB_KEYS:
        if d.get(k):
            d[k] = _scrub(d[k])
    return d


def _scrub_value(value: Any) -> Any:
    """Scrub free text in Generation.value (definitions, notes, nested detail)."""
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("samples", "sample", "values"):
                continue
            if k in _SCRUB_KEYS and isinstance(v, str):
                out[k] = _scrub(v)
            else:
                out[k] = _scrub_value(v)
        return out
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    return value


def _scrub_generation(gen: Generation) -> Generation:
    return replace(gen, detail=_scrub_detail(gen.detail), value=_scrub_value(gen.value))


def generations_from_detection(
    det: Any,
    *,
    run_id: str,
    fingerprint_key: str = "",
    ts: str = "",
) -> list[Generation]:
    """One generation per engine that actually ran — including 'looked and said no'."""
    ts = ts or _utc_now_iso()
    gens: list[Generation] = []

    def _emit(source: str, field: str, value: Any, confidence: Optional[float], detail: dict) -> None:
        gens.append(Generation(
            field=field,
            source=source,
            value=value,
            confidence=confidence,
            run_id=run_id,
            ts=ts,
            detail=detail,
            fingerprint_key=fingerprint_key,
        ))

    is_pii = bool(getattr(det, "detected", False))
    entity = getattr(det, "entity_type", None)

    presidio = getattr(det, "presidio_score", None)
    if presidio is not None:
        _emit(
            "regex", "pii",
            {"is_pii": is_pii if (presidio or 0) >= 0.5 else False, "entity_type": entity},
            float(presidio) if presidio is not None else None,
            {
                "decision_rule": getattr(det, "decision_path", None) or "",
                "regex_hits": list(getattr(det, "regex_hits", None) or [])[:10],
                "pattern": getattr(det, "presidio_pattern", None),
            },
        )
        if entity:
            _emit("regex", "entity_type", entity, float(presidio), {})

    gliner = getattr(det, "gliner_score", None)
    if gliner is not None:
        ner_pii = bool(gliner and float(gliner) >= 0.5)
        _emit(
            "ner", "pii",
            {"is_pii": ner_pii, "entity_type": getattr(det, "gliner_label", None) or entity},
            float(gliner),
            {
                "label": getattr(det, "gliner_label", None),
                "ner_hits": list(getattr(det, "ner_hits", None) or [])[:10],
                "ner_engine": getattr(det, "ner_engine", None),
            },
        )
        label = getattr(det, "gliner_label", None) or entity
        if label:
            _emit("ner", "entity_type", label, float(gliner), {})

    phone = getattr(det, "phone_score", None)
    if phone is not None:
        _emit(
            "phone", "pii",
            {"is_pii": bool(phone and float(phone) >= 0.5), "entity_type": getattr(det, "phone_entity", None)},
            float(phone),
            {
                "phone_entity": getattr(det, "phone_entity", None),
                "msisdn_valid_rate": getattr(det, "msisdn_valid_rate", None),
            },
        )

    llm_score = getattr(det, "llm_score", None)
    llm_verdict = getattr(det, "llm_verdict", None)
    llm_reasoning = getattr(det, "llm_reasoning", None)
    if llm_score is not None or llm_verdict is not None or llm_reasoning is not None:
        llm_pii = bool(is_pii) if llm_verdict in (None, "CONFIRMED") else False
        if str(llm_verdict or "").upper() == "REJECTED":
            llm_pii = False
        elif str(llm_verdict or "").upper() == "CONFIRMED":
            llm_pii = True
        _emit(
            "llm", "pii",
            {"is_pii": llm_pii, "verdict": llm_verdict, "entity_type": entity},
            float(llm_score) if llm_score is not None else None,
            {
                "verdict": llm_verdict,
                "reasoning": _scrub(llm_reasoning),
            },
        )
        if entity:
            _emit(
                "llm", "entity_type", entity,
                float(llm_score) if llm_score is not None else None,
                {"verdict": llm_verdict, "reasoning": _scrub(llm_reasoning)},
            )

    rule_ids = list(getattr(det, "edge_rule_ids", None) or [])
    if rule_ids:
        _emit(
            "custom_rule", "pii",
            {"is_pii": is_pii, "entity_type": entity},
            getattr(det, "confidence", None),
            {"rule_id": rule_ids[0], "rule_ids": rule_ids},
        )

    return gens


def generations_from_telemetry(
    column: str,
    tel: dict,
    *,
    run_id: str = "",
    fingerprint_key: str = "",
    ts: str = "",
) -> list[Generation]:
    """Backfill one generation per engine recorded in column telemetry."""
    ts = ts or _utc_now_iso()
    run_id = run_id or str(tel.get("run_id") or "backfill")
    entity = tel.get("entity_type")
    conf = tel.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf_f = None
    engines = list(tel.get("discovery_engines") or [])
    gens: list[Generation] = []

    def _emit(source: str, score_key: str, extra: dict | None = None) -> None:
        score = tel.get(score_key)
        try:
            score_f = float(score) if score is not None else conf_f
        except (TypeError, ValueError):
            score_f = conf_f
        is_pii = bool(entity) if score_f is None else bool(score_f >= 0.5 and entity)
        gens.append(Generation(
            field="pii",
            source=source,
            value={"is_pii": is_pii, "entity_type": entity},
            confidence=score_f,
            run_id=run_id,
            ts=ts,
            detail={
                "decision_rule": tel.get("decision_rule") or "",
                "backfill": True,
                **(extra or {}),
            },
            fingerprint_key=fingerprint_key,
        ))
        if entity:
            gens.append(Generation(
                field="entity_type",
                source=source,
                value=entity,
                confidence=score_f,
                run_id=run_id,
                ts=ts,
                detail={"backfill": True},
                fingerprint_key=fingerprint_key,
            ))

    mapping = {
        "regex": "presidio_score",
        "ner": "gliner_score",
        "phone": "phone_score",
        "llm": "llm_score",
    }
    emitted = set()
    for source, key in mapping.items():
        if source in engines or tel.get(key) is not None or (
            source == "llm" and (tel.get("llm_verdict") or tel.get("llm_reasoning"))
        ):
            extra = {}
            if source == "llm":
                extra = {
                    "verdict": tel.get("llm_verdict"),
                    "reasoning": _scrub(tel.get("llm_reasoning")),
                }
            if source == "regex" and tel.get("regex_hits"):
                extra["regex_hits"] = list(tel.get("regex_hits") or [])[:10]
            if source == "ner" and tel.get("ner_hits"):
                extra["ner_hits"] = list(tel.get("ner_hits") or [])[:10]
            _emit(source, key, extra)
            emitted.add(source)

    if tel.get("edge_rule_ids") or "custom_rule" in engines:
        gens.append(Generation(
            field="pii",
            source="custom_rule",
            value={"is_pii": bool(entity), "entity_type": entity},
            confidence=conf_f,
            run_id=run_id,
            ts=ts,
            detail={"rule_ids": list(tel.get("edge_rule_ids") or []), "backfill": True},
            fingerprint_key=fingerprint_key,
        ))

    if not gens and (entity or conf_f is not None or engines):
        gens.append(Generation(
            field="pii",
            source="regex",
            value={"is_pii": bool(entity), "entity_type": entity},
            confidence=conf_f,
            run_id=run_id,
            ts=ts,
            detail={"decision_rule": tel.get("decision_rule") or "", "backfill": True,
                    "discovery_engines": engines},
            fingerprint_key=fingerprint_key,
        ))
    _ = column
    return gens


def generations_from_contract_column(
    column: str,
    prop: dict,
    *,
    source: str,
    run_id: str,
    fingerprint_key: str = "",
    ts: str = "",
    confidence: float | None = None,
) -> list[Generation]:
    """Persist enrich / synthesis opinions so the steward can pick among them."""
    ts = ts or _utc_now_iso()
    gens: list[Generation] = []
    if source not in GENERATION_SET:
        return gens

    def _emit(field: str, value: Any, detail: dict | None = None) -> None:
        if value is None or value == "" or value == []:
            return
        gens.append(Generation(
            field=field,
            source=source,
            value=value,
            confidence=confidence,
            run_id=run_id,
            ts=ts,
            detail=dict(detail or {}),
            fingerprint_key=fingerprint_key,
        ))

    business = prop.get("business") if isinstance(prop.get("business"), dict) else {}
    definition = business.get("definition") or prop.get("description") or ""
    if isinstance(definition, dict):
        definition = definition.get("definition") or definition.get("purpose") or ""
    _emit("definition", str(definition).strip() if definition else "")

    tags = list(prop.get("tags") or [])
    if tags:
        _emit("tags", tags)

    classification = str(prop.get("classification") or "")
    privacy = prop.get("privacy") if isinstance(prop.get("privacy"), dict) else {}
    if not classification:
        classification = str(privacy.get("classification") or "")
    _emit("classification", classification)

    entity = prop.get("entity_type")
    pii_priv = privacy.get("pii") if isinstance(privacy.get("pii"), dict) else {}
    if not entity:
        entity = pii_priv.get("entity_type")
    if not entity:
        pii_block = prop.get("pii") if isinstance(prop.get("pii"), dict) else {}
        entity = pii_block.get("entity_type")
    _emit("entity_type", entity)

    is_pii = None
    try:
        from redibis.contracts.privacy import column_is_pii
        is_pii = bool(column_is_pii(prop))
    except Exception:
        is_pii = bool(entity) or str(classification).lower().startswith("pii")
    _emit("pii", {"is_pii": is_pii, "entity_type": entity})
    _ = column
    return gens


def generations_from_profile(
    column: str,
    stats: dict,
    *,
    run_id: str,
    fingerprint_key: str = "",
    ts: str = "",
) -> list[Generation]:
    ts = ts or _utc_now_iso()
    logical = stats.get("logical_type") or stats.get("inferred_class")
    gens: list[Generation] = []
    if logical:
        gens.append(Generation(
            field="logical_type",
            source="profile",
            value=logical,
            confidence=None,
            run_id=run_id,
            ts=ts,
            detail={
                "null_rate": stats.get("null_rate"),
                "ndv": stats.get("ndv") or stats.get("nunique"),
                "format_signature": stats.get("format_signature"),
            },
            fingerprint_key=fingerprint_key,
        ))
    _ = column
    return gens
