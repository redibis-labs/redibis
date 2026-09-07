"""Shared recognizers for column and free-text PII scanning."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from redibis.pii.regex_catalog import CATALOG
from redibis.pii.rules.ruleset import RuleSet
from redibis.pii.scan.result import Candidate

logger = logging.getLogger("pii.rules.recognizers")

_PHONE_ENTITY_TYPES = frozenset({"PHONE_NUMBER", "PHONE", "MSISDN"})
_CONTEXT_WINDOW = 40


def _pattern_name_from_result(r) -> str | None:
    """Extract catalog pattern key from a Presidio analyzer result."""
    if (
        hasattr(r, "analysis_explanation")
        and r.analysis_explanation
        and hasattr(r.analysis_explanation, "recognizer_name")
    ):
        return r.analysis_explanation.recognizer_name.replace("TelcoCatalog_", "")
    meta = getattr(r, "recognition_metadata", None) or {}
    if isinstance(meta, dict):
        rec_name = meta.get("recognizer_name") or meta.get("recognizer_identifier")
        if rec_name:
            return str(rec_name).replace("TelcoCatalog_", "")
    if hasattr(r, "analysis") and hasattr(r.analysis, "pattern"):
        return str(r.analysis.pattern)
    return None


@dataclass(frozen=True)
class ColumnRegexAggregate:
    """Aggregated column regex evidence before telecom postprocess."""

    regex_hits: tuple[dict, ...]
    match_count: int
    total: int


@dataclass(frozen=True)
class RecognizeContext:
    language: str = "en"
    arabic: bool = False
    group: str = "free_text"
    column_name: str = ""
    entities: tuple[str, ...] = ()


@runtime_checkable
class Recognizer(Protocol):
    name: str

    def recognize(self, text: str, ctx: RecognizeContext) -> list[Candidate]: ...


def _canonical(entity_type: str) -> str:
    from redibis.models import canonical_entity

    return canonical_entity(entity_type) or entity_type


def _entity_allowed(entity_type: str, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return True
    canon = _canonical(entity_type)
    allowed_set = {_canonical(e) for e in allowed}
    return canon in allowed_set or entity_type in allowed


def _context_boost(text: str, start: int, end: int, hints: tuple[str, ...]) -> bool:
    if not hints:
        return False
    left = max(0, start - _CONTEXT_WINDOW)
    right = min(len(text), end + _CONTEXT_WINDOW)
    window = text[left:right].lower()
    return any(h.lower() in window for h in hints if h)


class RegexRecognizer:
    """Presidio pattern recognizers backed by the compiled RuleSet catalog."""

    name = "regex"

    def __init__(self, ruleset: RuleSet):
        self._ruleset = ruleset
        self._engines: dict[tuple, object] = {}

    def _engine_key(
        self,
        group: str,
        arabic: bool,
        *,
        collision_suppressions: dict[str, str] | None = None,
        column_name_hints: tuple[str, ...] = (),
    ) -> tuple:
        return (
            group,
            arabic,
            tuple(sorted((collision_suppressions or {}).items())),
            column_name_hints,
        )

    def _engine(
        self,
        group: str,
        arabic: bool,
        *,
        collision_suppressions: dict[str, str] | None = None,
        column_name_hints: tuple[str, ...] = (),
    ):
        key = self._engine_key(
            group,
            arabic,
            collision_suppressions=collision_suppressions,
            column_name_hints=column_name_hints,
        )
        if key in self._engines:
            return self._engines[key]
        try:
            from presidio_analyzer import RecognizerRegistry
            from redibis.pii.presidio_nlp import build_pattern_analyzer_engine
            from redibis.pii.recognizer_factory import build_recognizers
        except ImportError:
            logger.warning("presidio-analyzer not installed — regex recognizer disabled")
            self._engines[key] = None
            return None

        try:
            recs = build_recognizers(
                group=group,
                arabic=arabic,
                active_collision_suppressions=collision_suppressions,
                column_name_hints=column_name_hints,
                regex_overrides=self._ruleset.regex_overrides,
            )
            registry = RecognizerRegistry()
            for r in recs:
                registry.add_recognizer(r)
            engine = build_pattern_analyzer_engine(registry, supported_languages=["en"])
        except Exception as exc:
            logger.warning("Failed to build Presidio engine: %s", exc)
            engine = None
        self._engines[key] = engine
        return engine

    def _text_engine(self, group: str, arabic: bool):
        """Presidio engine for free-text span scanning."""
        return self._engine(group, arabic)

    def recognize_values(
        self,
        values: list[str],
        ctx: RecognizeContext,
        *,
        collision_suppressions: dict[str, str] | None = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> ColumnRegexAggregate:
        """Column reducer — aggregate Presidio pattern hits across cell values."""
        from redibis.pii.telecom_signals import presidio_match_candidates

        empty = ColumnRegexAggregate(regex_hits=(), match_count=0, total=max(len(values), 1))
        hints = tuple(ctx.column_name.lower().split("_")) if ctx.column_name else ()
        engine = self._engine(
            ctx.group,
            ctx.arabic,
            collision_suppressions=collision_suppressions,
            column_name_hints=hints,
        )
        if engine is None:
            return empty

        hits: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "best_score": 0.0, "entity_type": None}
        )
        match_count = 0
        total = max(len(values), 1)
        catalog = self._ruleset.patterns

        for val in values:
            if not val or not isinstance(val, str) or val.upper() in ("NONE", "NAN", "N/A"):
                continue
            val_matched = False
            matched_patterns: set[str] = set()
            try:
                for candidate in presidio_match_candidates(val):
                    results = engine.analyze(text=candidate, language="en")
                    if not results:
                        continue
                    for r in results:
                        name = _pattern_name_from_result(r)
                        if not name:
                            continue
                        if name not in matched_patterns:
                            matched_patterns.add(name)
                            h = hits[name]
                            h["count"] += 1
                        else:
                            h = hits[name]
                        h["entity_type"] = r.entity_type
                        h["best_score"] = max(h["best_score"], r.score)
                        val_matched = True
            except Exception:
                continue
            if val_matched:
                match_count += 1

        regex_hits: list[dict] = []
        for name, h in hits.items():
            entry = catalog.get(name) or CATALOG.get(name)
            regex_hits.append({
                "pattern_name": name,
                "regex": getattr(entry, "pattern", "") if entry else "",
                "entity_type": h["entity_type"],
                "score": round(h["best_score"], 4),
                "match_rate": round(h["count"] / total, 4),
                "group": getattr(entry, "recognizer_group", "") if entry else "",
                "collision_group": getattr(entry, "collision_group", None) if entry else None,
                "validator": getattr(entry, "requires_validator", None) if entry else None,
            })
        regex_hits.sort(key=lambda x: x["score"], reverse=True)
        return ColumnRegexAggregate(
            regex_hits=tuple(regex_hits),
            match_count=match_count,
            total=total,
        )
    def recognize(self, text: str, ctx: RecognizeContext) -> list[Candidate]:
        if not text:
            return []
        engine = self._text_engine(ctx.group, ctx.arabic)
        if engine is None:
            return self._fallback_regex(text, ctx)

        try:
            results = engine.analyze(text=text, language="en")
        except Exception as exc:
            logger.warning("Presidio analyze failed: %s", exc)
            return self._fallback_regex(text, ctx)

        # Pattern hints + pack entity tokens (locale/tokens.yaml)
        hint_by_entity: dict[str, tuple[str, ...]] = {}
        for _name, entry in self._ruleset.patterns_for_group(ctx.group, arabic=ctx.arabic).items():
            hint_by_entity.setdefault(entry.entity_type, entry.context_hints)

        out: list[Candidate] = []
        for r in results:
            et = _canonical(getattr(r, "entity_type", "") or "")
            if not et or not _entity_allowed(et, ctx.entities):
                continue
            start = int(r.start)
            end = int(r.end)
            if start < 0 or end > len(text) or start >= end:
                continue
            matched = text[start:end]
            score = float(getattr(r, "score", 0) or 0)
            pattern_hints = hint_by_entity.get(getattr(r, "entity_type", ""), ()) or hint_by_entity.get(et, ())
            pack_hints = self._ruleset.context_hints_for_entity(et)
            hints = tuple(dict.fromkeys([*(pattern_hints or ()), *pack_hints]))
            boosted = _context_boost(text, start, end, hints)
            if boosted:
                score = min(1.0, score + 0.05)
            recognizer_name = ""
            meta = getattr(r, "recognition_metadata", None) or {}
            if isinstance(meta, dict):
                recognizer_name = str(meta.get("recognizer_name") or "")
            out.append(Candidate(
                entity_type=et,
                score=score,
                engine="regex",
                start=start,
                end=end,
                text=matched,
                recognizer=recognizer_name or "presidio",
                context_boost=boosted,
            ))
        return out

    def _fallback_regex(self, text: str, ctx: RecognizeContext) -> list[Candidate]:
        """Direct catalog scan when Presidio is unavailable."""
        out: list[Candidate] = []
        for name, entry in self._ruleset.patterns_for_group(ctx.group, arabic=ctx.arabic).items():
            et = _canonical(entry.entity_type)
            if not _entity_allowed(et, ctx.entities):
                continue
            try:
                compiled = re.compile(entry.pattern)
            except re.error:
                continue
            for m in compiled.finditer(text):
                start, end = m.start(), m.end()
                matched = text[start:end]
                score = float(entry.presidio_score)
                hints = tuple(dict.fromkeys([
                    *(entry.context_hints or ()),
                    *self._ruleset.context_hints_for_entity(et),
                ]))
                boosted = _context_boost(text, start, end, hints)
                if boosted:
                    score = min(1.0, score + 0.05)
                out.append(Candidate(
                    entity_type=et,
                    score=score,
                    engine="regex",
                    start=start,
                    end=end,
                    text=matched,
                    recognizer=name,
                    context_boost=boosted,
                ))
        return out


class PhoneRecognizer:
    """Validate phone-shaped candidates with libphonenumber."""

    name = "phone"

    def __init__(self, ruleset: RuleSet):
        self._ruleset = ruleset

    def recognize(
        self,
        text: str,
        ctx: RecognizeContext,
        *,
        seed_candidates: list[Candidate] | None = None,
    ) -> list[Candidate]:
        if not text:
            return []
        if not _entity_allowed("PHONE_NUMBER", ctx.entities):
            return []
        region = self._ruleset.region_for_language(ctx.language)
        seeds = seed_candidates or []
        candidates = [c for c in seeds if c.entity_type in _PHONE_ENTITY_TYPES]
        if not candidates:
            candidates = self._structural_candidates(text)

        out: list[Candidate] = []
        seen: set[tuple[int, int]] = set()
        for c in candidates:
            if c.start is None or c.end is None:
                continue
            key = (c.start, c.end)
            if key in seen:
                continue
            seen.add(key)
            matched = text[c.start:c.end]
            if not self._is_valid_phone(matched, region):
                continue
            out.append(Candidate(
                entity_type="PHONE_NUMBER",
                score=max(c.score, self._ruleset.thresholds.phone_min),
                engine="phone",
                start=c.start,
                end=c.end,
                text=matched,
                recognizer="phonenumbers",
                validator="phonenumbers:valid",
                context_boost=c.context_boost,
            ))
        return out

    def _structural_candidates(self, text: str) -> list[Candidate]:
        pattern = re.compile(
            r"(?<!\w)(?:\+?\d[\d\s().-]{6,18}\d)(?!\w)"
        )
        out: list[Candidate] = []
        for m in pattern.finditer(text):
            out.append(Candidate(
                entity_type="PHONE_NUMBER",
                score=0.5,
                engine="phone",
                start=m.start(),
                end=m.end(),
                text=m.group(0),
                recognizer="structural",
            ))
        return out

    @staticmethod
    def _is_valid_phone(value: str, region: str) -> bool:
        import phonenumbers
        from redibis.pii.phone_engine import _looks_like_date

        text = (value or "").strip()
        if not text or _looks_like_date(text):
            return False
        try:
            n = phonenumbers.parse(text, region)
            return bool(phonenumbers.is_valid_number(n))
        except Exception:
            return False


class NerRecognizer:
    """NER backend span extraction (GLiNER / remote)."""

    name = "ner"

    def __init__(self, ruleset: RuleSet, backend: object | None = None):
        self._ruleset = ruleset
        self._backend = backend

    def recognize(self, text: str, ctx: RecognizeContext) -> list[Candidate]:
        if not text or self._backend is None:
            return []
        analyze_text = getattr(self._backend, "analyze_text", None)
        if not callable(analyze_text):
            return []
        labels = list(self._ruleset.ner_labels)
        phrases = dict(self._ruleset.ner_phrases)
        try:
            spans = analyze_text(text, labels=labels, phrases=phrases)
        except TypeError:
            # Older backends without phrases= — still run
            try:
                spans = analyze_text(text, labels=labels)
            except Exception as exc:
                logger.warning("NER analyze_text failed: %s", exc)
                return []
        except Exception as exc:
            logger.warning("NER analyze_text failed: %s", exc)
            return []

        out: list[Candidate] = []
        for span in spans or []:
            # NERSpan dataclass or dict
            if hasattr(span, "start"):
                start = int(span.start)
                end = int(span.end)
                label = str(getattr(span, "label", "") or "")
                score = float(getattr(span, "score", 0) or 0)
                matched = getattr(span, "text", None) or text[start:end]
                model = str(getattr(span, "model", "") or getattr(self._backend, "name", "ner"))
            elif isinstance(span, dict):
                start = int(span.get("start", -1))
                end = int(span.get("end", -1))
                label = str(span.get("label") or "")
                score = float(span.get("score") or 0)
                matched = span.get("text") or (text[start:end] if start >= 0 else "")
                model = str(span.get("model") or getattr(self._backend, "name", "ner"))
            else:
                continue
            if start < 0 or end > len(text) or start >= end:
                continue
            if text[start:end] != matched and matched:
                # Prefer source-slice validation
                if text[start:end]:
                    matched = text[start:end]
                else:
                    continue
            et = self._map_ner_label(label)
            if not et or not _entity_allowed(et, ctx.entities):
                continue
            if score < self._ruleset.thresholds.gliner_min * 0.5:
                # Soft floor; SpanResolver applies min_score later
                pass
            out.append(Candidate(
                entity_type=et,
                score=score,
                engine="ner",
                start=start,
                end=end,
                text=matched,
                recognizer=model,
            ))
        return out

    def _map_ner_label(self, label: str) -> str:
        """Map GLiNER phrase or raw label back to a canonical entity type."""
        raw = (label or "").strip()
        if not raw:
            return ""
        low = raw.lower()
        for canon, phrase in self._ruleset.ner_phrases.items():
            if str(phrase).lower() == low:
                return _canonical(str(canon))
        return _canonical(raw.upper().replace(" ", "_")) or ""

    def recognize_values(self, values: list[str], ctx: RecognizeContext) -> list[Candidate]:
        """Column reducer — aggregate NER hits across values (analyze_values path)."""
        if not values or self._backend is None:
            return []
        analyze = getattr(self._backend, "analyze", None)
        if not callable(analyze):
            return []
        labels = list(self._ruleset.ner_labels)
        col = ctx.column_name or "_col"
        try:
            report = analyze(values, col, labels=labels)
        except Exception as exc:
            logger.warning("NER analyze_values failed: %s", exc)
            return []
        hits = getattr(report, "hits", None) or []
        out: list[Candidate] = []
        for hit in hits:
            if hasattr(hit, "label"):
                label = str(hit.label)
                score = float(getattr(hit, "score", 0) or 0)
            elif isinstance(hit, dict):
                label = str(hit.get("label") or "")
                score = float(hit.get("score") or 0)
            else:
                continue
            et = self._map_ner_label(label)
            if not et or not _entity_allowed(et, ctx.entities):
                continue
            out.append(Candidate(
                entity_type=et,
                score=score,
                engine="ner",
                start=None,
                end=None,
                text=None,
                recognizer=str(getattr(self._backend, "name", "ner")),
            ))
        return out
