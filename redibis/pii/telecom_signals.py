"""Column-level telecom PII signals — normalizer, geo/SIM gates, evidence stats."""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from redibis.models import PIIDetection

from redibis.pii.regex_catalog import (
    CATALOG,
    catalog_validators,
    normalize_msisdn_egypt,
    validate_iccid,
    validate_luhn,
    validate_egypt_national_id,
)

_DEFAULT_PREFIXES = ("010", "011", "012", "015")
_MSISDN_NORMALIZER_KEY = "msisdn_egypt_normalized"
_MSISDN_NORMALIZER_SCORE = 0.92

_LAT_TOKENS = frozenset({"lat", "latitude", "خط_عرض"})
_LON_TOKENS = frozenset({"lon", "lng", "longitude", "خط_طول"})
_GEO_PATTERNS = frozenset({
    "gps_latitude", "gps_longitude", "gps_pair",
    "gps_scaled_int", "wkt_point", "gps_dms",
})
_IMSI_STRICT = "imsi_strict"
_FIFTEEN_DIGIT_PATTERNS = frozenset(
    {"imei", "imsi_strict", "imsi_egypt", "imsi_generic", "iccid", "iccid_egypt"}
)


def _col_tokens(column_name: str) -> set[str]:
    return set(re.split(r"[^a-z0-9؀-ۿ]+", column_name.lower()))


def _has_context_hint(column_name: str, pattern_name: str | None) -> bool:
    if not pattern_name:
        return False
    entry = CATALOG.get(pattern_name)
    if not entry:
        return False
    hints = tuple(getattr(entry, "context_hints", ()) or ())
    from redibis.pii.context_tokens import (
        builtin_context_tokens,
        column_matches_hints,
        effective_hints_for_pattern,
    )

    merged = effective_hints_for_pattern(
        hints, entry.entity_type, builtin_context_tokens()
    )
    if not merged:
        return False
    return column_matches_hints(column_name, merged)


def _is_non_null_value(val: Any) -> bool:
    if val is None:
        return False
    s = str(val).strip()
    return bool(s) and s.upper() not in {"NONE", "NAN", "N/A", "NULL"}


def compute_msisdn_valid_rate(
    values: list[Any],
    prefixes: tuple[str, ...] | list[str] | None = None,
) -> float:
    """Fraction of non-null values that normalize to a valid Egyptian MSISDN."""
    non_null = [v for v in values if _is_non_null_value(v)]
    if not non_null:
        return 0.0
    valid = sum(1 for v in non_null if normalize_msisdn_egypt(v) is not None)
    return valid / len(non_null)


def presidio_match_candidates(val: str) -> list[str]:
    """Raw value plus canonical MSISDN form for Presidio matching."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        if text and text not in seen:
            out.append(text)
            seen.add(text)

    _add(val)
    canon = normalize_msisdn_egypt(val)
    if canon:
        _add(canon)
        if canon.startswith("+20") and len(canon) == 13:
            _add("0" + canon[3:])
    return out


def merge_msisdn_normalizer_hit(
    regex_hits: list[dict],
    msisdn_valid_rate: float,
) -> list[dict]:
    """Inject a high-precision normalizer hit when the column is mostly valid MSISDN."""
    if msisdn_valid_rate <= 0:
        return regex_hits
    hit = {
        "pattern_name": _MSISDN_NORMALIZER_KEY,
        "regex": "normalize_msisdn_egypt",
        "entity_type": "PHONE_NUMBER",
        "score": round(_MSISDN_NORMALIZER_SCORE, 4),
        "match_rate": round(msisdn_valid_rate, 4),
        "group": "structured",
        "collision_group": None,
        "validator": None,
        "source": "normalizer",
    }
    merged = list(regex_hits)
    merged.append(hit)
    merged.sort(key=lambda x: (x.get("score") or 0, x.get("match_rate") or 0), reverse=True)
    return merged


def _phonenumbers_gate_passed(
    *,
    use_phonenumbers: bool,
    phone_valid_rate: float | None,
    phone_min: float,
    phonenumbers_ran: bool = False,
) -> bool:
    """
    When libphonenumber ran, PHONE_NUMBER requires column valid_rate ≥ τ.
    When gated off or disabled, callers fall back to MSISDN-plan rates.
    """
    if not use_phonenumbers:
        return True
    if phone_valid_rate is not None:
        return phone_valid_rate >= phone_min
    if phonenumbers_ran:
        return False
    return True


def apply_phone_phonenumbers_gate(
    regex_hits: list[dict],
    *,
    use_phonenumbers: bool,
    phone_valid_rate: float | None,
    phone_min: float,
    phonenumbers_ran: bool = False,
    column_name: str = "",
) -> list[dict]:
    """Drop PHONE_NUMBER regex hits when libphonenumber rejects the column."""
    from redibis.pii.phone_engine import name_suggests_network_identifier

    if name_suggests_network_identifier(column_name):
        return [h for h in regex_hits if not _is_phone_label(h.get("entity_type"))]
    if _phonenumbers_gate_passed(
        use_phonenumbers=use_phonenumbers,
        phone_valid_rate=phone_valid_rate,
        phone_min=phone_min,
        phonenumbers_ran=phonenumbers_ran,
    ):
        return regex_hits
    return [h for h in regex_hits if not _is_phone_label(h.get("entity_type"))]


def resolve_entity_type(
    *,
    presidio_score: float | None,
    presidio_entity: str | None,
    ner_score: float | None,
    ner_entity: str | None,
    phone_score: float | None,
    phone_entity: str | None,
    use_phonenumbers: bool,
    phone_valid_rate: float | None,
    phone_min: float,
    phonenumbers_ran: bool = False,
    column_name: str = "",
) -> str | None:
    """Pick entity from engine scores; PHONE_NUMBER requires the phonenumbers gate."""
    from redibis.pii.phone_engine import name_suggests_network_identifier

    if name_suggests_network_identifier(column_name):
        pairs: list[tuple[float, str | None]] = []
        if presidio_score is not None and presidio_entity and not _is_phone_label(presidio_entity):
            pairs.append((presidio_score, presidio_entity))
        if ner_score is not None and ner_entity and not _is_phone_label(ner_entity):
            pairs.append((ner_score, ner_entity))
        if not pairs:
            return None
        return max(pairs, key=lambda x: x[0])[1]

    pairs = []
    if presidio_score is not None and presidio_entity:
        pairs.append((presidio_score, presidio_entity))
    if ner_score is not None and ner_entity:
        pairs.append((ner_score, ner_entity))
    if phone_score is not None and phone_entity:
        pairs.append((phone_score, phone_entity))
    if not pairs:
        return presidio_entity or ner_entity or phone_entity

    entity = max(pairs, key=lambda x: x[0])[1]
    if _is_phone_label(entity) and not _phonenumbers_gate_passed(
        use_phonenumbers=use_phonenumbers,
        phone_valid_rate=phone_valid_rate,
        phone_min=phone_min,
        phonenumbers_ran=phonenumbers_ran,
    ):
        non_phone = [
            (s, e) for s, e in pairs
            if e and not _is_phone_label(e)
        ]
        if non_phone:
            return max(non_phone, key=lambda x: x[0])[1]
        return None
    return entity


def compute_phone_plan_score(
    msisdn_valid_rate: float,
    phone_stats: dict,
    *,
    column_name: str = "",
    msisdn_valid_rate_min: float = 0.80,
    presidio_entity: str | None = None,
    presidio_score: float | None = None,
    ner_label: str | None = None,
    ner_score: float | None = None,
    presidio_min: float = 0.80,
    ner_min: float = 0.70,
    use_phonenumbers: bool = True,
) -> tuple[Optional[float], Optional[str]]:
    """
    Return (phone_score, entity_type) for the equation engine.

    ``phone_score`` is the best available plan-valid rate in [0, 1].
    ``entity_type`` is PHONE_NUMBER only when rate clears the floor AND
    column context or multi-engine agreement supports a verdict (§3.2).
    """
    from redibis.models import canonical_entity

    rates: list[float] = []
    if msisdn_valid_rate > 0:
        rates.append(msisdn_valid_rate)
    if phone_stats.get("available"):
        mobile = float(phone_stats.get("mobile_rate") or 0)
        valid = float(phone_stats.get("valid_rate") or 0)
        if mobile > 0:
            rates.append(mobile)
        if valid > 0:
            rates.append(valid)
    if not rates:
        return None, None
    score = max(rates)
    if score < msisdn_valid_rate_min:
        return score, None

    pn_ran = bool(phone_stats.get("available"))
    pn_rate = float(phone_stats.get("valid_rate") or 0) if pn_ran else None
    if use_phonenumbers and pn_ran and not _phonenumbers_gate_passed(
        use_phonenumbers=True,
        phone_valid_rate=pn_rate,
        phone_min=msisdn_valid_rate_min,
        phonenumbers_ran=True,
    ):
        return pn_rate if pn_rate is not None else score, None

    if phone_verdict_eligible(
        column_name=column_name,
        phone_score=score,
        phone_min=msisdn_valid_rate_min,
        presidio_entity=presidio_entity,
        presidio_score=presidio_score,
        ner_label=ner_label,
        ner_score=ner_score,
        presidio_min=presidio_min,
        ner_min=ner_min,
    ):
        return score, "PHONE_NUMBER"
    return score, None


def _is_phone_label(label: str | None) -> bool:
    if not label:
        return False
    from redibis.models import canonical_entity

    canon = canonical_entity(label.upper().replace(" ", "_"))
    if canon == "PHONE_NUMBER":
        return True
    low = label.lower()
    return any(tok in low for tok in ("phone", "mobile", "msisdn", "tel"))


def count_phone_engine_agreements(
    *,
    phone_score: float | None,
    phone_min: float,
    presidio_entity: str | None,
    presidio_score: float | None,
    presidio_min: float,
    ner_label: str | None,
    ner_score: float | None,
    ner_min: float,
) -> int:
    """How many engines independently vote phone at or above their floors."""
    n = 0
    if phone_score is not None and phone_score >= phone_min:
        n += 1
    if presidio_score is not None and presidio_score >= presidio_min:
        if _is_phone_label(presidio_entity):
            n += 1
    if ner_score is not None and ner_score >= ner_min and _is_phone_label(ner_label):
        n += 1
    return n


def presidio_phone_signal(
    detection: PIIDetection | None = None,
    *,
    regex_hits: list | None = None,
) -> tuple[str | None, float | None]:
    """Best regex/presidio phone hit (independent of final ``entity_type``)."""
    hits = regex_hits if regex_hits is not None else (detection.regex_hits if detection else [])
    best_score = 0.0
    best_entity: str | None = None
    for hit in hits or []:
        entity = hit.get("entity_type")
        if not _is_phone_label(entity):
            continue
        score = float(hit.get("score") or 0)
        if score > best_score:
            best_score = score
            best_entity = entity
    if best_entity:
        return best_entity, best_score
    return None, None


def phone_verdict_eligible(
    *,
    column_name: str,
    phone_score: float | None,
    phone_min: float,
    presidio_entity: str | None = None,
    presidio_score: float | None = None,
    ner_label: str | None = None,
    ner_score: float | None = None,
    presidio_min: float = 0.80,
    ner_min: float = 0.70,
    use_phonenumbers: bool = True,
    phone_valid_rate: float | None = None,
    phonenumbers_ran: bool = False,
) -> bool:
    """Shape alone never decides — name context or ≥2 engines (§3.2)."""
    from redibis.pii.phone_engine import (
        name_has_phone_token,
        name_suggests_identifier,
        name_suggests_network_identifier,
    )

    if name_suggests_network_identifier(column_name):
        return False

    if name_suggests_identifier(column_name) and not name_has_phone_token(column_name):
        return False

    if use_phonenumbers and phonenumbers_ran and not _phonenumbers_gate_passed(
        use_phonenumbers=True,
        phone_valid_rate=phone_valid_rate,
        phone_min=phone_min,
        phonenumbers_ran=True,
    ):
        return False

    if name_has_phone_token(column_name):
        if phone_score is not None and phone_score >= phone_min:
            return True
        if presidio_score is not None and presidio_score >= presidio_min and _is_phone_label(presidio_entity):
            return True
        if ner_score is not None and ner_score >= ner_min and _is_phone_label(ner_label):
            return True
        return False

    if phone_score is None or phone_score < phone_min:
        return False
    return count_phone_engine_agreements(
        phone_score=phone_score,
        phone_min=phone_min,
        presidio_entity=presidio_entity,
        presidio_score=presidio_score,
        presidio_min=presidio_min,
        ner_label=ner_label,
        ner_score=ner_score,
        ner_min=ner_min,
    ) >= 2


def find_geo_partner_columns(columns: list[str]) -> dict[str, Optional[str]]:
    """Map lat/lon-ish column names to their partner column when detectable."""
    from redibis.pii.geo_engine import find_geo_partners

    return find_geo_partners(columns)


def _value_has_decimal(val: str) -> bool:
    return bool(re.search(r"\.\d+", val.strip()))


def _in_egypt_geofence(lat: float, lon: float) -> bool:
    from redibis.pii.geofences import in_geofence

    return in_geofence("egypt", lat, lon)


def _geo_values_signals(
    values: list[str],
    *,
    partner_values: Optional[list] = None,
    geofence: str = "egypt",
) -> dict[str, Any]:
    """Column geo stats. Geofence counts require *partner_values* (G3/G5 fix)."""
    from redibis.pii.geo_engine import geo_column_signals

    sig = geo_column_signals(
        values,
        partner_values=partner_values,
        geofence=geofence,
    )
    return {
        "checked": sig.checked,
        "decimal_fraction_rate": (
            1.0 if sig.decimal_places_median > 0 and sig.checked else 0.0
        ),
        "decimal_places_median": sig.decimal_places_median,
        "integer_only_rate": (
            1.0 - (1.0 if sig.decimal_places_median > 0 else 0.0)
            if sig.checked else 0.0
        ),
        "lat_in_range_rate": sig.lat_in_range_rate,
        "lon_in_range_rate": sig.lon_in_range_rate,
        "in_range": sig.lat_in_range_rate if sig.range_signal != "none" else 0.0,
        "abs_max": sig.abs_max,
        "null_island_rate": sig.null_island_rate,
        "distinct_rate": sig.distinct_rate,
        "egypt_geofence_hits": sig.egypt_geofence_hits,
        "geofence_rate": sig.geofence_rate,
        "range_signal": sig.range_signal,
        "precision_signal": sig.precision_signal,
    }


def apply_geo_hardening(
    regex_hits: list[dict],
    values: list[str],
    column_name: str,
    *,
    geo_partner: Optional[str] = None,
    partner_values: Optional[list] = None,
    geo_require_pair: bool = True,
    geo_egypt_geofence: bool = False,
    geofence: str = "egypt",
) -> list[dict]:
    """Drop or penalize geo hits that lack partner/corroboration (invariant 22)."""
    from redibis.pii.geo_engine import (
        detect_coordinate_scale,
        geo_column_signals,
        geo_confidence,
        geo_verdict,
        name_has_geo_token,
    )

    if not regex_hits:
        return regex_hits

    has_partner = geo_partner is not None and partner_values is not None
    sig = geo_column_signals(
        values,
        partner_values=partner_values if has_partner else None,
        geofence=geofence,
    )
    # Fill name signal from column.
    from dataclasses import replace
    sig = replace(sig, name_signal=name_has_geo_token(column_name), partner_signal=has_partner)

    if has_partner:
        scale = detect_coordinate_scale(
            values, partner_values, geofence=geofence,
        )
        if scale is not None:
            sig = replace(sig, scaled_integer=scale)

    verdict = geo_verdict(sig, require_pair=geo_require_pair)
    conf = geo_confidence(sig, require_pair=geo_require_pair)

    out: list[dict] = []
    for hit in regex_hits:
        pname = hit.get("pattern_name") or ""
        if pname not in _GEO_PATTERNS and pname not in {
            "gps_scaled_int", "wkt_point", "gps_dms",
        }:
            out.append(hit)
            continue

        # Self-evidencing formats: pair / WKT / DMS bypass the lone-column ban.
        if pname in {"gps_pair", "wkt_point", "gps_dms"}:
            new_hit = dict(hit)
            score = float(new_hit.get("score") or 0)
            if geo_egypt_geofence and pname == "gps_pair" and sig.geofence_rate >= 0.80:
                score = min(1.0, score + 0.10)
            elif geo_egypt_geofence and has_partner and sig.geofence_rate >= 0.80:
                score = min(1.0, score + 0.10)
            new_hit["score"] = round(score, 4)
            # Self-evidencing → confidence at least the pattern score floor.
            new_hit["geo_confidence"] = max(conf, float(new_hit.get("score") or 0.85))
            new_hit["geofence_rate"] = sig.geofence_rate
            out.append(new_hit)
            continue

        if verdict == "none":
            continue

        new_hit = dict(hit)
        score = float(new_hit.get("score") or 0)
        # G5: geofence boost for any pair-backed hit, not only gps_pair.
        if geo_egypt_geofence and has_partner and sig.geofence_rate >= 0.80:
            score = min(1.0, score + 0.10)
        if sig.scaled_integer is not None and pname == "gps_scaled_int":
            score = max(score, 0.70)
        new_hit["score"] = round(score, 4)
        new_hit["geo_confidence"] = conf
        new_hit["geofence_rate"] = sig.geofence_rate
        new_hit["geo_verdict"] = verdict
        out.append(new_hit)
    return out

def _fifteen_digit_class(val: str) -> str | None:
    digits = re.sub(r"\D", "", str(val))
    if len(digits) not in (14, 15):
        return None
    # 14-digit Egyptian NIDs share the digit length with truncated IMSIs and
    # their century+year prefix often coincides with a real MCC. Prefer NID
    # when the structural validator holds — never label a valid NID as IMSI.
    if len(digits) == 14:
        nid = validate_egypt_national_id(digits)
        ok = nid.get("valid", False) if isinstance(nid, dict) else bool(nid)
        if ok:
            return None
    # MCC-known IMSI wins over Luhn coincidence (S4).
    from redibis.pii.subscriber_id import validate_imsi

    imsi = validate_imsi(digits)
    if imsi.valid:
        return "IMSI"
    if len(digits) == 15 and digits.startswith("89") and validate_iccid(digits):
        return "ICCID"
    if len(digits) == 15 and validate_luhn(digits):
        from redibis.pii.device_id import validate_imei
        if validate_imei(digits):
            return "IMEI"
        # Luhn alone without RBI — weak IMEI signal
        return "IMEI"
    return None


def apply_sim_gates(
    regex_hits: list[dict],
    values: list[str],
    column_name: str,
) -> list[dict]:
    """Checksum gates, IMSI strict tightening, and 15-digit disambiguation."""
    if not regex_hits:
        return regex_hits

    class_counts: Counter[str] = Counter()
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        kind = _fifteen_digit_class(str(raw))
        if kind:
            class_counts[kind] += 1

    dominant_15: str | None = None
    if class_counts:
        dominant_15 = class_counts.most_common(1)[0][0]

    imsi_strict_context = _has_context_hint(column_name, _IMSI_STRICT)
    out: list[dict] = []
    for hit in regex_hits:
        pname = hit.get("pattern_name") or ""
        entity = hit.get("entity_type") or ""

        if pname == _IMSI_STRICT:
            has_602 = any(
                re.sub(r"\D", "", str(v)).startswith("602")
                for v in values
                if _is_non_null_value(v)
            )
            if not has_602 and not imsi_strict_context:
                continue

        if pname in _FIFTEEN_DIGIT_PATTERNS and dominant_15:
            expected_entity = dominant_15
            if entity and entity != expected_entity:
                if not _has_context_hint(column_name, pname):
                    continue

        # MCC-first: when IMSI and IMEI both fire, keep IMSI if MCC known.
        if entity == "IMEI" and dominant_15 == "IMSI":
            if not _has_context_hint(column_name, pname):
                continue
        if entity == "IMSI" and dominant_15 == "IMEI":
            if not _has_context_hint(column_name, pname):
                # Keep IMSI if any value has a known MCC.
                from redibis.pii.subscriber_id import validate_imsi
                any_imsi = any(
                    validate_imsi(re.sub(r"\D", "", str(v))).valid
                    for v in values if _is_non_null_value(v)
                )
                if not any_imsi:
                    continue

        validator_name = hit.get("validator")
        imei_name_context = (
            pname in {"imei", "imei_with_dashes"}
            and _has_context_hint(column_name, pname)
        )
        if validator_name and not imei_name_context:
            entry = CATALOG.get(pname)
            vname = validator_name or getattr(entry, "requires_validator", None)
            if vname:
                fn = catalog_validators.get(vname)
                if fn:
                    passes = 0
                    checked = 0
                    for raw in values:
                        if not _is_non_null_value(raw):
                            continue
                        digits = re.sub(r"\D", "", str(raw))
                        if not digits:
                            continue
                        checked += 1
                        try:
                            res = fn(digits)
                            ok = res.get("valid", False) if isinstance(res, dict) else bool(res)
                        except Exception:
                            ok = False
                        if ok:
                            passes += 1
                    if checked and (passes / checked) < 0.50:
                        continue

        out.append(hit)
    return out


def _prefer_nid_over_imsi(hits: list[dict]) -> list[dict]:
    """When both EG_NATIONAL_ID and IMSI fire, keep NID if it matches at least as often.

    Presidio maps any truthy ``validate_result`` to MAX_SCORE, so IMSI and NID
    both land at 1.0 after validation — match_rate is the disambiguator.
    """
    nid_hits = [h for h in hits if (h.get("entity_type") or "") == "EG_NATIONAL_ID"]
    imsi_hits = [h for h in hits if (h.get("entity_type") or "") == "IMSI"]
    if not nid_hits or not imsi_hits:
        return hits
    best_nid = max(nid_hits, key=lambda h: (h.get("match_rate") or 0, h.get("score") or 0))
    best_imsi = max(imsi_hits, key=lambda h: (h.get("match_rate") or 0, h.get("score") or 0))
    if (best_nid.get("match_rate") or 0) >= (best_imsi.get("match_rate") or 0):
        return [h for h in hits if (h.get("entity_type") or "") != "IMSI"]
    return hits


def postprocess_regex_hits(
    regex_hits: list[dict],
    values: list[str],
    column_name: str,
    *,
    msisdn_valid_rate: float = 0.0,
    geo_partner: Optional[str] = None,
    partner_values: Optional[list] = None,
    geo_require_pair: bool = True,
    geo_egypt_geofence: bool = False,
) -> list[dict]:
    """Full telecom post-processing pipeline for regex hits."""
    hits = merge_msisdn_normalizer_hit(regex_hits, msisdn_valid_rate)
    hits = apply_sim_gates(hits, values, column_name)
    hits = apply_geo_hardening(
        hits,
        values,
        column_name,
        geo_partner=geo_partner,
        partner_values=partner_values,
        geo_require_pair=geo_require_pair,
        geo_egypt_geofence=geo_egypt_geofence,
    )
    hits = _prefer_nid_over_imsi(hits)
    hits.sort(key=lambda x: (x.get("score") or 0, x.get("match_rate") or 0), reverse=True)
    return hits


def msisdn_format_breakdown(values: list[Any]) -> dict[str, int]:
    """Count how many values match common Egyptian MSISDN surface forms."""
    counts = {
        "bare_10": 0,
        "leading_zero_11": 0,
        "bare_20_12": 0,
        "plus_20": 0,
        "double_zero": 0,
        "spaced_or_dashed": 0,
        "excel_float": 0,
        "normalized_valid": 0,
    }
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        s = str(raw).strip()
        if normalize_msisdn_egypt(raw) is not None:
            counts["normalized_valid"] += 1
        digits = re.sub(r"\D", "", s)
        if re.search(r"[\s\-]", s) and digits:
            counts["spaced_or_dashed"] += 1
        if s.endswith(".0"):
            counts["excel_float"] += 1
        if s.startswith("+20"):
            counts["plus_20"] += 1
        elif digits.startswith("0020"):
            counts["double_zero"] += 1
        elif digits.startswith("20") and len(digits) == 12:
            counts["bare_20_12"] += 1
        elif digits.startswith("0") and len(digits) == 11:
            counts["leading_zero_11"] += 1
        elif len(digits) == 10 and re.match(r"^1[0125]\d{8}$", digits):
            counts["bare_10"] += 1
    return counts


def prefix_histogram(values: list[Any], prefixes: tuple[str, ...] | list[str]) -> dict[str, float]:
    """Share of values whose canonical MSISDN starts with each prefix."""
    pref_list = list(prefixes or _DEFAULT_PREFIXES)
    counts = {p: 0 for p in pref_list}
    total = 0
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        canon = normalize_msisdn_egypt(raw)
        if not canon or len(canon) < 5:
            continue
        total += 1
        national = "0" + canon[3:]
        for p in pref_list:
            if national.startswith(p):
                counts[p] = counts.get(p, 0) + 1
                break
    if not total:
        return {p: 0.0 for p in pref_list}
    return {p: counts[p] / total for p in pref_list}


def length_distribution(values: list[Any]) -> dict[str, int]:
    dist: Counter[str] = Counter()
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        digits = re.sub(r"\D", "", str(raw))
        if digits:
            dist[str(len(digits))] += 1
    return dict(dist)


def checksum_pass_rates(values: list[Any]) -> dict[str, float]:
    luhn_pass = iccid_pass = nid_pass = 0
    luhn_n = iccid_n = nid_n = 0
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        digits = re.sub(r"\D", "", str(raw))
        if not digits:
            continue
        if len(digits) == 15:
            luhn_n += 1
            if validate_luhn(digits):
                luhn_pass += 1
        if digits.startswith("89") and 17 <= len(digits) <= 20:
            iccid_n += 1
            if validate_iccid(digits):
                iccid_pass += 1
        if len(digits) == 14:
            nid_n += 1
            res = validate_egypt_national_id(digits)
            ok = res.get("valid", False) if isinstance(res, dict) else bool(res)
            if ok:
                nid_pass += 1
    return {
        "luhn_pass_rate": (luhn_pass / luhn_n) if luhn_n else 0.0,
        "iccid_pass_rate": (iccid_pass / iccid_n) if iccid_n else 0.0,
        "egypt_nid_pass_rate": (nid_pass / nid_n) if nid_n else 0.0,
    }
