"""Declared, versioned normalization for value-tier PII span matching.

Reuse ``fold_ar`` / ``fold_indic_digits`` and ``token_normalize.apply_normalizers``.
A ``value`` F1 without a profile id is not reproducible.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from redibis.pii.text_preprocess.expanders._util import fold_ar, fold_indic_digits
from redibis.pii.token_normalize import apply_normalizers

PROFILE_V1_ID = "v1"

DIGIT_FAMILY = frozenset({
    "PHONE_NUMBER",
    "PHONE",
    "MSISDN",
    "EG_NATIONAL_ID",
    "NATIONAL_ID",
    "CREDIT_CARD",
    "PARTIAL_CARD",
    "IMEI",
    "IMSI",
    "ICCID",
    "OTP",
    "SIM_PUK",
    "VOUCHER",
})
ARABIC_TEXT_FAMILY = frozenset({
    "PERSON",
    "NAME",
    "LOCATION",
    "ADDRESS",
    "ORGANIZATION",
    "NRP",
})
EMAIL_FAMILY = frozenset({"EMAIL_ADDRESS", "EMAIL"})
MAC_IP_FAMILY = frozenset({"MAC_ADDRESS", "IP_ADDRESS", "IPV4", "IPV6"})

# Include Arabic question mark (U+061F) so spoken/address tails strip like "?".
_EDGE_PUNCT = (
    ".,:;،؛!?؟…·•\"'“”‘’«»()[]{}<>/"
    "\u00a0"
)
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_WHITESPACE = re.compile(r"\s+")
_YEHS = str.maketrans({"\u0649": "\u064a", "\u06cc": "\u064a"})
_NON_DIGIT = re.compile(r"\D+")
_ARABIC_ALEF = str.maketrans({
    "\u0622": "\u0627",
    "\u0623": "\u0627",
    "\u0625": "\u0627",
    "\u0671": "\u0627",
})
_TEH_MARBUTA = str.maketrans({"\u0629": "\u0647"})
_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670]")
_PREPROCESS_KINDS = frozenset({
    "arabic_spoken_digits",
    "parenthesized_digits",
    "digit_cluster",
    "spaced_email",
    "age_phrase",
    "labeled_secret",
})

# v1 arabic_text is the independent steps that are *not* already inside fold_ar.
# fold_ar = casefold + alef + teh_marbuta + diacritics + tatweel.
PROFILE_V1: dict[str, Any] = {
    "profile": PROFILE_V1_ID,
    "all": ["trim", "collapse_whitespace", "strip_edge_punct"],
    "digit_family": ["fold_arabic_indic", "digits_only"],
    "arabic_text": [
        "fold_ar",
        "strip_zero_width",
        "unify_yeh",
    ],
    "email": ["casefold", "strip_spaces"],
    "mac_ip": ["casefold", "strip_spaces"],
    "never_normalize_across_families": True,
}

CANONICAL_MATCH_YES = "yes"
CANONICAL_MATCH_NO = "no"
CANONICAL_MATCH_UNKNOWN = "unknown"
CANONICAL_MATCH_NA = "n/a"


class NormalizationError(ValueError):
    """Unknown profile or step."""


def _strip_edge_punct(text: str) -> str:
    return (text or "").strip(_EDGE_PUNCT + " \t\r\n")


def _apply_step(text: str, step: str) -> str:
    name = str(step or "").strip()
    if not name or name == "identity":
        return text or ""
    if name == "trim":
        return (text or "").strip()
    if name == "collapse_whitespace":
        return _WHITESPACE.sub(" ", text or "").strip()
    if name == "strip_edge_punct":
        return _strip_edge_punct(text)
    if name == "fold_arabic_indic":
        return fold_indic_digits(text)
    if name == "digits_only":
        return _NON_DIGIT.sub("", fold_indic_digits(text))
    if name == "fold_ar":
        return fold_ar(text)
    if name == "strip_tatweel":
        return (text or "").replace("\u0640", "")
    if name == "strip_diacritics":
        return _DIACRITICS.sub("", text or "")
    if name == "strip_zero_width":
        return _ZERO_WIDTH.sub("", text or "")
    if name == "unify_alef":
        return (text or "").translate(_ARABIC_ALEF)
    if name == "unify_teh_marbuta":
        return (text or "").translate(_TEH_MARBUTA)
    if name == "unify_yeh":
        return (text or "").translate(_YEHS)
    if name == "casefold":
        return apply_normalizers(text or "", ("casefold",))
    if name == "strip_spaces":
        return re.sub(r"\s+", "", text or "")
    raise NormalizationError(f"unknown normalization step {name!r}")


def _family_key(entity_type: str) -> str:
    et = str(entity_type or "").strip().upper()
    if et in DIGIT_FAMILY:
        return "digit_family"
    if et in ARABIC_TEXT_FAMILY:
        return "arabic_text"
    if et in EMAIL_FAMILY:
        return "email"
    if et in MAC_IP_FAMILY:
        return "mac_ip"
    return "all"


def normalize_value(
    text: str,
    *,
    entity_type: str = "",
    profile: Mapping[str, Any] | None = None,
) -> str:
    """Apply the declared profile for ``entity_type``'s family."""
    spec = dict(profile or PROFILE_V1)
    out = text or ""
    for step in spec.get("all") or []:
        out = _apply_step(out, str(step))
    family = _family_key(entity_type)
    if family != "all":
        for step in spec.get(family) or []:
            out = _apply_step(out, str(step))
    return out


def normalize_surface(
    text: str,
    span: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> str:
    """Normalize the span's surface with only the profile's ``all`` steps.

    Digit-family ``digits_only`` would wipe spoken Arabic words to ``""``.
    Surface-to-surface fallbacks therefore skip family steps.
    """
    start = int(span["start"])
    end = int(span["end"])
    return normalize_value(
        (text or "")[start:end],
        entity_type="",
        profile=profile,
    )


def span_canonical(span: Mapping[str, Any]) -> str:
    """Return assembled digits/value when present on the span or recognizer."""
    raw = span.get("canonical")
    if raw not in (None, ""):
        return str(raw)
    rec = str(span.get("recognizer") or "")
    parts = rec.split("|")
    if len(parts) < 2:
        return ""
    if any(part in _PREPROCESS_KINDS for part in parts[:-1]):
        return parts[-1]
    return ""


def span_compare_value(
    text: str,
    span: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> str:
    """Surface text, or ``canonical`` when the span carries an assembled value."""
    canon = span_canonical(span)
    if canon:
        raw = canon
    else:
        start = int(span["start"])
        end = int(span["end"])
        raw = (text or "")[start:end]
    return normalize_value(raw, entity_type=str(span.get("entity_type") or ""), profile=profile)


def compare_span_values(
    text: str,
    expected: Mapping[str, Any],
    predicted: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Compare gold vs prediction. Return ``(equal, canonical_match)``.

    ``canonical_match`` is ``yes`` / ``no`` when both sides have canonical,
    ``unknown`` when only gold has it (surface fallback), else ``n/a``.
    """
    spec = profile or PROFILE_V1
    entity_type = str(expected.get("entity_type") or predicted.get("entity_type") or "")
    gold_c = span_canonical(expected)
    pred_c = span_canonical(predicted)

    if gold_c and pred_c:
        left = normalize_value(gold_c, entity_type=entity_type, profile=spec)
        right = normalize_value(pred_c, entity_type=entity_type, profile=spec)
        ok = bool(left) and left == right
        return ok, CANONICAL_MATCH_YES if ok else CANONICAL_MATCH_NO

    if gold_c and not pred_c:
        # Never compare gold canonical digits against a spoken/word surface.
        gold_s = normalize_surface(text, expected, profile=spec)
        pred_s = normalize_surface(text, predicted, profile=spec)
        if gold_s and gold_s == pred_s:
            return True, CANONICAL_MATCH_UNKNOWN
        pred_family = normalize_value(
            (text or "")[int(predicted["start"]): int(predicted["end"])],
            entity_type=entity_type,
            profile=spec,
        )
        gold_digits = normalize_value(gold_c, entity_type=entity_type, profile=spec)
        if gold_digits and gold_digits == pred_family:
            return True, CANONICAL_MATCH_UNKNOWN
        return False, CANONICAL_MATCH_UNKNOWN

    left = span_compare_value(text, expected, profile=spec)
    right = normalize_value(
        (text or "")[int(predicted["start"]): int(predicted["end"])],
        entity_type=entity_type,
        profile=spec,
    )
    if pred_c:
        right = normalize_value(pred_c, entity_type=entity_type, profile=spec)
    return bool(left) and left == right, CANONICAL_MATCH_NA


def profile_search_paths(profile_id: str) -> list[Path]:
    here = Path(__file__).resolve().parent / "profiles" / f"{profile_id}.yaml"
    bundled = Path(__file__).resolve().parents[3] / "configs" / f"eval_normalization.{profile_id}.yaml"
    return [here, bundled]


@lru_cache(maxsize=8)
def load_profile(profile_id: str = PROFILE_V1_ID) -> dict[str, Any]:
    """Load a named profile. Unknown ids raise; corrupt files raise; ``v1`` always resolves."""
    wanted = str(profile_id or PROFILE_V1_ID).strip() or PROFILE_V1_ID
    loaded = _load_yaml_profile(wanted)
    if loaded:
        return loaded
    if wanted == PROFILE_V1_ID:
        return dict(PROFILE_V1)
    raise NormalizationError(f"unknown normalization profile {wanted!r}")


def _load_yaml_profile(profile_id: str) -> dict[str, Any] | None:
    for path in profile_search_paths(profile_id):
        if not path.is_file():
            continue
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise NormalizationError(
                f"invalid normalization profile {path}: {exc}"
            ) from exc
        if not isinstance(data, dict) or not data:
            raise NormalizationError(
                f"invalid normalization profile {path}: expected a mapping"
            )
        out = dict(PROFILE_V1) if profile_id == PROFILE_V1_ID else {}
        out.update(data)
        out["profile"] = str(data.get("profile") or profile_id)
        return out
    return None


def profile_id_of(profile: Mapping[str, Any] | None) -> str:
    if not profile:
        return PROFILE_V1_ID
    return str(profile.get("profile") or PROFILE_V1_ID)
