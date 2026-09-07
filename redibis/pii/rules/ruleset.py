"""Compiled PII RuleSet — single source of patterns, labels, and thresholds.

``RuleSetCompiler.default()`` mirrors today's catalog / NER / threshold constants.
``RuleSetCompiler.from_stack()`` / ``from_pack()`` compile the same surface from an
``AppliedPackStack`` or ``LoadedPack`` (locale/regex, tokens, phone, NER phrases).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from redibis.pii.ner_backend import DEFAULT_NER_LABELS, _GLINER_PHRASE_BY_CANONICAL
from redibis.pii.regex_catalog import CATALOG, PatternEntry
from redibis.pii.regex_overrides import RegexOverrides
from redibis.pii.thresholds import Thresholds

_LANGUAGE_REGION: dict[str, str] = {
    "en": "US",
    "ar": "EG",
    "fr": "FR",
    "de": "DE",
    "es": "ES",
    "it": "IT",
    "pt": "BR",
    "nl": "NL",
    "tr": "TR",
}

# BCP-47 language → Faker locale (de-id fake strategy).
_LANGUAGE_FAKER: dict[str, str] = {
    "en": "en_US",
    "ar": "ar_AA",
    "fr": "fr_FR",
    "de": "de_DE",
    "es": "es_ES",
    "it": "it_IT",
    "pt": "pt_BR",
    "nl": "nl_NL",
    "tr": "tr_TR",
}


@dataclass(frozen=True)
class SpecialRule:
    """Declarative telecom / locale special-case rule (predicate + effect)."""

    id: str
    kind: str
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleSet:
    """Immutable compiled detection rules."""

    id: str
    version: str
    patterns: Mapping[str, PatternEntry]
    context_tokens: Mapping[str, tuple[str, ...]]
    ner_labels: tuple[str, ...]
    ner_phrases: Mapping[str, str]
    special_rules: tuple[SpecialRule, ...]
    thresholds: Thresholds
    default_region: str = "EG"
    phone_regions: Mapping[str, str] = field(default_factory=lambda: dict(_LANGUAGE_REGION))
    faker_locales: Mapping[str, str] = field(default_factory=lambda: dict(_LANGUAGE_FAKER))
    # Retained so RegexRecognizer can rebuild Presidio with the same overrides.
    regex_overrides: Optional[RegexOverrides] = None

    def patterns_for_group(self, group: str, *, arabic: bool = False) -> dict[str, PatternEntry]:
        out: dict[str, PatternEntry] = {}
        for name, entry in self.patterns.items():
            if not entry.active:
                continue
            if entry.recognizer_group != group:
                continue
            if entry.script == "arabic" and not arabic:
                continue
            out[name] = entry
        return out

    def region_for_language(self, language: str) -> str:
        lang = (language or "en").split("-")[0].lower()
        return self.phone_regions.get(lang) or self.default_region

    def locale_for_language(self, language: str) -> str:
        """Faker / de-id locale for a BCP-47 language tag."""
        lang = (language or "en").split("-")[0].lower()
        return self.faker_locales.get(lang) or self.faker_locales.get("en") or "en_US"

    def context_hints_for_entity(self, entity_type: str) -> tuple[str, ...]:
        return tuple(self.context_tokens.get(entity_type) or ())

    def entity_catalogue(self) -> list[dict]:
        """Entity types with which engines can detect them (for /entities)."""
        by_entity: dict[str, set[str]] = {}
        for entry in self.patterns.values():
            if not entry.active:
                continue
            et = entry.entity_type
            by_entity.setdefault(et, set()).add("regex")
            if et in ("PHONE_NUMBER", "PHONE", "MSISDN"):
                by_entity[et].add("phone")
        for label in self.ner_labels:
            from redibis.models import canonical_entity

            key = label.strip().upper().replace(" ", "_")
            canon = canonical_entity(key) or key
            by_entity.setdefault(canon, set()).add("ner")
        # Phone always available as a gate engine
        by_entity.setdefault("PHONE_NUMBER", set()).update({"regex", "phone", "ner"})

        family_map = {
            "PERSON": "identity",
            "NAME": "identity",
            "ADDRESS": "identity",
            "LOCATION": "identity",
            "EMAIL_ADDRESS": "contact",
            "EMAIL": "contact",
            "PHONE_NUMBER": "telecom",
            "PHONE": "telecom",
            "MSISDN": "telecom",
            "CREDIT_CARD": "financial",
            "IBAN": "financial",
            "IBAN_CODE": "financial",
            "BANK_ACCOUNT": "financial",
            "NATIONAL_ID": "government_id",
            "PASSPORT": "government_id",
            "SSN": "government_id",
            "EG_NATIONAL_ID": "government_id",
        }
        rows = []
        for et in sorted(by_entity):
            rows.append({
                "entity_type": et,
                "engines": sorted(by_entity[et]),
                "family": family_map.get(et, "other"),
            })
        return rows


class RuleSetCompiler:
    """Compile a RuleSet from pack data or built-in defaults."""

    @staticmethod
    def compile_patterns(
        overrides: Optional[RegexOverrides] = None,
    ) -> dict[str, PatternEntry]:
        """Canonical catalog compile — single source for patterns + overrides."""
        from redibis.pii.regex_catalog import CATALOG, _dict_to_pattern_entry

        if overrides is None or not overrides:
            return dict(CATALOG)

        if overrides.replace_all:
            base: dict[str, PatternEntry] = {}
        else:
            base = dict(CATALOG)

        for name, entry_dict in overrides.add.items():
            base[name] = _dict_to_pattern_entry(entry_dict)

        for name in overrides.remove:
            base.pop(name, None)

        return base

    @staticmethod
    def default(
        *,
        regex_overrides: Optional[RegexOverrides] = None,
        thresholds: Optional[Thresholds] = None,
        default_region: str = "EG",
        ner_labels: Optional[list[str]] = None,
        ner_phrases: Optional[Mapping[str, str]] = None,
        context_tokens: Optional[Mapping[str, tuple[str, ...]]] = None,
        faker_locales: Optional[Mapping[str, str]] = None,
        ruleset_id: str = "builtin-default",
        version: str = "1.0.0",
    ) -> RuleSet:
        catalog = RuleSetCompiler.compile_patterns(regex_overrides)
        if context_tokens is not None:
            context = {k: tuple(v) for k, v in context_tokens.items()}
        else:
            from redibis.pii.context_tokens import builtin_context_tokens

            context = builtin_context_tokens()

        labels = tuple(ner_labels or DEFAULT_NER_LABELS)
        phrases = dict(_GLINER_PHRASE_BY_CANONICAL)
        if ner_phrases:
            phrases.update({str(k): str(v) for k, v in ner_phrases.items()})
        thr = thresholds or Thresholds()
        regions = dict(_LANGUAGE_REGION)
        if default_region:
            # Primary pack language: ar when EG, else first letter pair of region.
            if default_region.upper() == "EG":
                regions["ar"] = "EG"
            else:
                regions.setdefault(default_region[:2].lower(), default_region.upper())
                regions["ar"] = regions.get("ar", "EG")

        faker = dict(_LANGUAGE_FAKER)
        if faker_locales:
            faker.update({str(k): str(v) for k, v in faker_locales.items()})

        return RuleSet(
            id=ruleset_id,
            version=version,
            patterns=dict(catalog),
            context_tokens=context,
            ner_labels=labels,
            ner_phrases=phrases,
            special_rules=(
                SpecialRule(id="exclude_network_ids", kind="phone_exclude_network"),
            ),
            thresholds=thr,
            default_region=default_region or "EG",
            phone_regions=regions,
            faker_locales=faker,
            regex_overrides=regex_overrides,
        )

    @classmethod
    def from_stack(
        cls,
        stack: Any,
        *,
        ruleset_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> RuleSet:
        """Compile from an ``AppliedPackStack`` (active pack resolution result)."""
        from redibis.pack.stack_models import AppliedPackStack

        if not isinstance(stack, AppliedPackStack):
            raise TypeError(f"expected AppliedPackStack, got {type(stack).__name__}")

        cfg = stack.config
        pii = cfg.pii
        overrides = stack.regex_overrides
        if overrides is None and getattr(pii, "regex_overrides", None):
            overrides = RegexOverrides.from_dict(pii.regex_overrides)

        region = getattr(pii, "default_region", None) or "EG"
        phone = dict(stack.phone_locale or {})
        regions_list = phone.get("default_regions") or []
        if regions_list:
            region = str(regions_list[0]) or region

        thr = getattr(pii, "thresholds", None) or Thresholds()
        labels = None
        ner = getattr(pii, "ner", None)
        if ner and getattr(ner, "labels", None):
            labels = list(ner.labels)

        faker_locales = dict(_LANGUAGE_FAKER)
        mask_loc = getattr(getattr(cfg, "masking", None), "default_locale", None)
        if mask_loc and str(mask_loc) not in ("", "default"):
            # Bind pack masking locale to primary language inferred from region.
            lang = "ar" if str(region).upper() == "EG" else str(region)[:2].lower()
            faker_locales[lang] = str(mask_loc)
            faker_locales.setdefault("en", str(mask_loc) if lang == "en" else faker_locales["en"])

        phone_regions = dict(_LANGUAGE_REGION)
        phone_regions[("ar" if str(region).upper() == "EG" else str(region)[:2].lower())] = str(
            region
        ).upper()
        # Keep explicit language→region from pack phone doc when multiple listed.
        for r in regions_list:
            r_s = str(r).upper()
            phone_regions[r_s[:2].lower() if r_s != "EG" else "ar"] = r_s

        # Layer identity for audit
        rid = ruleset_id
        ver = version
        if stack.layers:
            top = stack.layers[-1]
            rid = rid or f"pack:{top.id}"
            ver = ver or top.version
        rid = rid or "pack-stack"
        ver = ver or "1.0.0"

        # Seed regions into default() via phone_regions after construction
        rs = cls.default(
            regex_overrides=overrides,
            thresholds=thr,
            default_region=str(region),
            ner_labels=labels,
            ner_phrases=dict(stack.ner_phrases or {}),
            context_tokens=dict(stack.context_tokens) if stack.context_tokens else None,
            faker_locales=faker_locales,
            ruleset_id=rid,
            version=ver,
        )
        # Replace phone_regions with pack-aware map (frozen → rebuild)
        return RuleSet(
            id=rs.id,
            version=rs.version,
            patterns=rs.patterns,
            context_tokens=rs.context_tokens,
            ner_labels=rs.ner_labels,
            ner_phrases=rs.ner_phrases,
            special_rules=rs.special_rules,
            thresholds=rs.thresholds,
            default_region=rs.default_region,
            phone_regions=phone_regions,
            faker_locales=rs.faker_locales,
            regex_overrides=rs.regex_overrides,
        )

    @classmethod
    def from_pack(cls, pack: object, **kwargs) -> RuleSet:
        """Compile from ``LoadedPack`` or ``AppliedPackStack``.

        ``LoadedPack`` is applied as a single overlay onto ``RedibisConfig.default()``
        (with builtin context-token seed) so locale sections take effect.
        """
        from redibis.pack.stack_models import AppliedPackStack

        if isinstance(pack, AppliedPackStack):
            return cls.from_stack(pack, **kwargs)

        # LoadedPack — apply as one layer
        from redibis.config import RedibisConfig
        from redibis.pack.models import LoadedPack
        from redibis.pack.resolver import _apply_loaded_pack
        from redibis.pii.context_tokens import builtin_context_tokens

        if not isinstance(pack, LoadedPack):
            raise TypeError(
                f"from_pack expects LoadedPack or AppliedPackStack, got {type(pack).__name__}"
            )

        base = kwargs.pop("base", None) or RedibisConfig.default()
        seed = kwargs.pop("seed_builtin_tokens", True)
        stack = AppliedPackStack(config=base)
        if seed:
            stack.context_tokens = builtin_context_tokens()
        stack = _apply_loaded_pack(
            stack,
            pack,
            mode=getattr(pack.manifest, "mode", None) or "overlay",
            path=None,
            source="loaded",
        )
        meta = pack.manifest.metadata
        return cls.from_stack(
            stack,
            ruleset_id=kwargs.pop("ruleset_id", None) or f"pack:{meta.id}",
            version=kwargs.pop("version", None) or meta.version,
            **kwargs,
        )
