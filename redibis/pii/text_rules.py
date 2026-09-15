"""Operator-updatable Text Gateway rule overlay.

The shipped regex catalogue stays read-only. Operators add exclusions, patterns,
sentence-level context cues, and quantity units via ``text_gateway.rules``,
``GET/PUT /api/pii/text/rules``, or a pack later. Compiled into ``RuleSet``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from redibis.pii.regex_overrides import RegexOverrides


@dataclass(frozen=True)
class ContextCue:
    """Keyword cues that classify or extend a nearby span."""

    entity_type: str
    triggers: tuple[str, ...] = ()
    extend: str = ""  # "" | "sentence"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"triggers": list(self.triggers)}
        if self.extend:
            out["extend"] = self.extend
        return out

    @classmethod
    def from_mapping(cls, entity_type: str, raw: Any) -> "ContextCue":
        if isinstance(raw, ContextCue):
            return raw
        if isinstance(raw, (list, tuple)):
            return cls(entity_type=entity_type, triggers=tuple(str(x) for x in raw if str(x).strip()))
        if not isinstance(raw, dict):
            return cls(entity_type=entity_type)
        triggers = raw.get("triggers") or raw.get("tokens") or []
        return cls(
            entity_type=entity_type,
            triggers=tuple(str(x) for x in triggers if str(x).strip()),
            extend=str(raw.get("extend") or ""),
        )


def _merge_regex(
    left: Optional[RegexOverrides],
    right: Optional[RegexOverrides],
) -> Optional[RegexOverrides]:
    if left is None or not left:
        return right
    if right is None or not right:
        return left
    if right.replace_all:
        return right
    add = dict(left.add or {})
    add.update(right.add or {})
    remove = list(dict.fromkeys(list(left.remove or []) + list(right.remove or [])))
    return RegexOverrides(add=add, remove=remove, replace_all=bool(left.replace_all))


def _unique(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = str(item).strip()
        if not key:
            continue
        folded = key.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(key)
    return tuple(out)


@dataclass(frozen=True)
class TextRuleOverlay:
    """Additive detection policy for free-text / Text Gateway scans."""

    exclude_terms: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    patterns: Optional[RegexOverrides] = None
    context_cues: Mapping[str, ContextCue] = field(default_factory=dict)
    quantity_units: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        cues = {
            et: cue.to_dict()
            for et, cue in self.context_cues.items()
        }
        return {
            "exclude_terms": list(self.exclude_terms),
            "exclude_patterns": list(self.exclude_patterns),
            "patterns": self.patterns.to_dict() if self.patterns else {"add": {}, "remove": [], "replace_all": False},
            "context_cues": cues,
            "quantity_units": list(self.quantity_units),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TextRuleOverlay":
        if isinstance(data, TextRuleOverlay):
            return data
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("text rules overlay must be a mapping")
        raw_patterns = data.get("patterns") or {}
        patterns = None
        if raw_patterns:
            patterns = (
                raw_patterns
                if isinstance(raw_patterns, RegexOverrides)
                else RegexOverrides.from_dict(raw_patterns)
            )
            if not patterns.add and not patterns.remove and not patterns.replace_all:
                patterns = None
        cues_raw = data.get("context_cues") or {}
        cues: dict[str, ContextCue] = {}
        if isinstance(cues_raw, dict):
            for key, value in cues_raw.items():
                et = str(key).strip().upper().replace(" ", "_")
                if not et:
                    continue
                cues[et] = ContextCue.from_mapping(et, value)
        return cls(
            exclude_terms=_unique(tuple(str(x) for x in (data.get("exclude_terms") or []))),
            exclude_patterns=_unique(tuple(str(x) for x in (data.get("exclude_patterns") or []))),
            patterns=patterns,
            context_cues=cues,
            quantity_units=_unique(tuple(str(x) for x in (data.get("quantity_units") or []))),
        )

    def merge(self, other: Optional["TextRuleOverlay"]) -> "TextRuleOverlay":
        if other is None:
            return self
        cues = dict(self.context_cues)
        for et, cue in other.context_cues.items():
            prior = cues.get(et)
            if prior is None:
                cues[et] = cue
            else:
                cues[et] = ContextCue(
                    entity_type=et,
                    triggers=_unique(prior.triggers + cue.triggers),
                    extend=cue.extend or prior.extend,
                )
        return TextRuleOverlay(
            exclude_terms=_unique(self.exclude_terms + other.exclude_terms),
            exclude_patterns=_unique(self.exclude_patterns + other.exclude_patterns),
            patterns=_merge_regex(self.patterns, other.patterns),
            context_cues=cues,
            quantity_units=_unique(self.quantity_units + other.quantity_units),
        )

    def triggers_for(self, entity_type: str) -> tuple[str, ...]:
        cue = self.context_cues.get(entity_type)
        return cue.triggers if cue else ()

    def all_triggers(self) -> list[tuple[str, str, str]]:
        """Return (entity_type, trigger, extend) rows."""
        rows: list[tuple[str, str, str]] = []
        for et, cue in self.context_cues.items():
            for trig in cue.triggers:
                rows.append((et, trig, cue.extend))
        return rows


def default_text_rules() -> TextRuleOverlay:
    """Shipped defaults that cover Egyptian call-center operator goldens."""
    return TextRuleOverlay.from_dict({
        "exclude_terms": [
            "agent", "caller", "migrate", "plan", "postpaid", "prepaid",
            "control", "router", "ticket", "request", "profile",
            "ok", "yes", "no",
        ],
        "exclude_patterns": [
            r"(?i)^agent:?$",
            r"(?i)^caller:?$",
        ],
        "patterns": {
            "add": {
                "arabic_name_after_label": {
                    "pattern": (
                        r"(?:اسمي|واسمي|باسم|اسمها|اسمه|اسم المفوض|واسم المفوض)\s+"
                        r"([\u0621-\u064A]+(?:\s[\u0621-\u064A]+){1,4}?)"
                        r"(?=\s+و|\s+(?:من|في|يا|على|لو)\b|[.،,؟!?]|$)"
                    ),
                    "entity_type": "PERSON",
                    "recognizer_group": "free_text",
                    "script": "arabic",
                    "presidio_score": 0.78,
                    "context_hints": ("اسم", "اسمي", "باسم", "اسمها"),
                    "unvalidated_reason": (
                        "Arabic full name after an explicit name cue "
                        "(اسمي / باسم / اسمها) is strong free-text evidence."
                    ),
                },
                "arabic_agent_name_after_maak": {
                    "pattern": r"(?:معاك|معك)\s+([\u0621-\u064A]{2,20})(?=\s+من\b)",
                    "entity_type": "PERSON",
                    "recognizer_group": "free_text",
                    "script": "arabic",
                    "presidio_score": 0.78,
                    "context_hints": ("معاك", "معك"),
                    "unvalidated_reason": (
                        "Agent given name after معاك/معك … من is strong "
                        "call-center evidence."
                    ),
                },
                "latin_name_after_label": {
                    "pattern": (
                        r"(?i)(?:it['’]s|it is|i am|i['’]m|name is)\s+"
                        r"([A-Z][a-z]+(?:[\s\-'][A-Z][a-z]+){1,4})"
                    ),
                    "entity_type": "PERSON",
                    "recognizer_group": "free_text",
                    "script": "latin",
                    "presidio_score": 0.78,
                    "context_hints": ("name",),
                    "unvalidated_reason": (
                        "English full name after it's / name is is strong "
                        "free-text evidence."
                    ),
                },
                "spoken_card_expiry": {
                    "pattern": (
                        r"شهر\s+[\u0621-\u064A]+"
                        r"(?:\s*\(\d{1,2}\))?"
                        r"(?:\s+سنة)?"
                        r"(?:\s+[\u0621-\u064A]+)+"
                        r"(?:\s*\(\d{2,4}\))?"
                    ),
                    "entity_type": "CREDIT_CARD_EXPIRATION",
                    "recognizer_group": "free_text",
                    "script": "arabic",
                    "presidio_score": 0.82,
                    "context_hints": ("انتهاء", "expiry", "شهر"),
                    "unvalidated_reason": (
                        "Spoken month/year expiry after شهر is shape evidence "
                        "in card-update transcripts."
                    ),
                },
                "arabic_health_condition": {
                    "pattern": r"حالة\s+ولادة\s+مستعجلة|ولادة\s+مستعجلة",
                    "entity_type": "GDPR_SPECIAL_CATEGORY",
                    "recognizer_group": "free_text",
                    "script": "arabic",
                    "presidio_score": 0.82,
                    "context_hints": ("صحة", "ولادة", "إسعاف"),
                    "unvalidated_reason": (
                        "An explicit emergency health phrase naming a medical "
                        "condition is special-category evidence."
                    ),
                },
                "arabic_company_name": {
                    "pattern": (
                        r"شركة\s+[\u0621-\u064A]+(?:\s[\u0621-\u064A]+){1,8}"
                    ),
                    "entity_type": "ORGANIZATION",
                    "recognizer_group": "free_text",
                    "script": "arabic",
                    "presidio_score": 0.8,
                    "context_hints": ("شركة", "حساب"),
                    "unvalidated_reason": (
                        "Egyptian company name after شركة is strong "
                        "organization evidence in B2B transcripts."
                    ),
                },
                "arabic_landmark_after_quddam": {
                    "pattern": r"قدام\s+[\u0621-\u064A]+(?:\s[\u0621-\u064A]+){0,3}",
                    "entity_type": "LOCATION",
                    "recognizer_group": "free_text",
                    "script": "arabic",
                    "presidio_score": 0.8,
                    "context_hints": ("عنوان", "لوكيشن"),
                    "unvalidated_reason": (
                        "Landmark after قدام is a physical location in "
                        "emergency geolocation transcripts."
                    ),
                },
                "arabic_desert_road_km": {
                    "pattern": (
                        r"طريق\s+[\u0621-\u064A]+(?:\s[\u0621-\u064A]+){1,5}"
                        r"\s+الكيلو\s+[\u0621-\u064A]+(?:\s+و[\u0621-\u064A]+)?"
                    ),
                    "entity_type": "LOCATION",
                    "recognizer_group": "free_text",
                    "script": "arabic",
                    "presidio_score": 0.82,
                    "context_hints": ("طريق", "لوكيشن"),
                    "unvalidated_reason": (
                        "Named road plus kilometre marker is a physical "
                        "location."
                    ),
                },
            },
            "remove": [],
            "replace_all": False,
        },
        "context_cues": {
            "LOCATION": {
                "triggers": [
                    "العنوان", "عنوان", "delivery address", "shipping address",
                    "اللوكيشن", "لوكيشن",
                ],
                "extend": "sentence",
            },
            "PHONE_NUMBER": {
                "triggers": [
                    "الخط", "رقم", "موبايل", "تليفون", "تواصل", "contact",
                    "msisdn", "phone", "mobile", "الموبايل",
                ],
            },
            "EG_NATIONAL_ID": {
                "triggers": [
                    "رقم قومي", "الرقم القومي", "National ID", "قومي", "nid",
                ],
            },
            "SIM_PUK": {
                "triggers": ["puk", "puk code", "رمز puk", "الـ puk", "باك"],
            },
            "VOUCHER": {
                "triggers": [
                    "scratch card", "scratch", "كارت شحن", "كود الشحن", "14 رقم",
                ],
            },
            "SUPPORT_TICKET": {
                "triggers": ["ticket", "تذكرة", "SR-"],
            },
        },
        "quantity_units": [
            "GB", "TB", "MB", "KB", "جنيه", "جنية", "EGP", "LE",
            "أيام", "يوم", "أسبوع", "اسابيع", "week", "weeks",
            "شهر", "months", "days", "minutes", "دقيقة",
        ],
    })


def merge_text_rules(*layers: Any) -> TextRuleOverlay:
    """Fold mappings / overlays left-to-right onto an empty overlay."""
    out = TextRuleOverlay()
    for layer in layers:
        if not layer:
            continue
        parsed = layer if isinstance(layer, TextRuleOverlay) else TextRuleOverlay.from_dict(layer)
        out = out.merge(parsed)
    return out


def compile_text_rules(*layers: Any) -> TextRuleOverlay:
    """Builtin defaults, then operator layers (config / API / pack)."""
    return merge_text_rules(default_text_rules(), *layers)


def overlay_from_pack_document(doc: Any) -> TextRuleOverlay:
    """Interpret a ``text_gateway/rules/*.yaml`` mapping as an overlay."""
    if isinstance(doc, TextRuleOverlay):
        return doc
    if not doc:
        return TextRuleOverlay()
    if not isinstance(doc, dict):
        raise ValueError("text_gateway rule document must be a mapping")
    if isinstance(doc.get("rules"), dict):
        return TextRuleOverlay.from_dict(doc["rules"])
    skip = {
        "id",
        "name",
        "version",
        "description",
        "author",
        "kind",
        "apiVersion",
        "metadata",
    }
    payload = {k: v for k, v in doc.items() if k not in skip}
    return TextRuleOverlay.from_dict(payload)


def compile_layered_text_rules(
    *,
    pack_docs: Sequence[Any] = (),
    pack_source_labels: Sequence[str] = (),
    config_rules: Any = None,
    persisted_rules: Any = None,
    draft_rules: Any = None,
    include_builtin: bool = True,
) -> tuple[TextRuleOverlay, tuple[str, ...]]:
    """Fold rule layers in Plan 2 precedence and name each contributing source.

    Order: builtin → pack docs (layer order) → config → persisted → draft.
    """
    sources: list[str] = []
    layers: list[Any] = []
    if include_builtin:
        layers.append(default_text_rules())
        sources.append("builtin")
    for index, doc in enumerate(pack_docs):
        if not doc:
            continue
        layers.append(overlay_from_pack_document(doc))
        if index < len(pack_source_labels) and pack_source_labels[index]:
            sources.append(str(pack_source_labels[index]))
        else:
            sources.append("pack")
    if config_rules:
        layers.append(config_rules)
        sources.append("config")
    if persisted_rules:
        layers.append(persisted_rules)
        sources.append("persisted")
    if draft_rules:
        layers.append(draft_rules)
        sources.append("draft")
    if not layers:
        return TextRuleOverlay(), ()
    return merge_text_rules(*layers), tuple(sources)
