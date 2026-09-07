"""Geolocation signals — partner + corroboration (never lone-column LOCATION).

Pure module: stdlib + optional geofence helper. No Presidio.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from redibis.pii.geofences import in_geofence

_COL_SPLIT = re.compile(r"[^a-z0-9\u0600-\u06ff]+", re.IGNORECASE)

_LAT_NAME_TOKENS = frozenset({
    "lat", "latitude", "خط_عرض",
})
_LON_NAME_TOKENS = frozenset({
    "lon", "lng", "longitude", "خط_طول",
})
# ``long`` is intentionally omitted — it fires on ``long_description``.
# Callers that need it must also see a geo-family token (gps/coord/…).
_LON_AMBIGUOUS = frozenset({"long"})
_GEO_NAME_TOKENS = frozenset({
    "coord", "gps", "geo", "position", "الموقع", "موقع",
}) | _LAT_NAME_TOKENS | _LON_NAME_TOKENS

# Tokens that must NOT fire lat/lon name signals (substring traps).
_LAT_FALSE_FRIENDS = frozenset({
    "latency", "lateral", "platform", "translation", "relative", "population",
})
_LON_FALSE_FRIENDS = frozenset({
    "long_description", "longtext", "longitude_desc", "along", "belong",
})


_NULLISH = frozenset({"NONE", "NAN", "N/A", "NULL", ""})


@dataclass(frozen=True)
class GeoSignals:
    name_signal: bool
    partner_signal: bool
    range_signal: str  # "latitude" | "longitude" | "both" | "none"
    precision_signal: bool
    geofence_rate: float
    scaled_integer: Optional[int]
    null_island_rate: float
    decimal_places_median: float = 0.0
    abs_max: float = 0.0
    distinct_rate: float = 0.0
    lat_in_range_rate: float = 0.0
    lon_in_range_rate: float = 0.0
    egypt_geofence_hits: int = 0
    checked: int = 0


def _tokens(col_name: str) -> set[str]:
    blob = (col_name or "").lower()
    return {t for t in _COL_SPLIT.split(blob) if t}


def _is_non_null(val: object) -> bool:
    if val is None:
        return False
    s = str(val).strip()
    return bool(s) and s.upper() not in _NULLISH


def name_has_geo_token(col_name: str) -> bool:
    """Token-boundary lat/lon/geo name signal (no latency / long_description)."""
    blob = (col_name or "").lower()
    toks = _tokens(col_name)
    if any(ff in blob for ff in _LAT_FALSE_FRIENDS | _LON_FALSE_FRIENDS):
        return bool(toks & (_LAT_NAME_TOKENS | _LON_NAME_TOKENS | (
            _GEO_NAME_TOKENS - _LON_AMBIGUOUS
        )))
    if toks & (_LAT_NAME_TOKENS | _LON_NAME_TOKENS):
        return True
    if toks & _LON_AMBIGUOUS:
        # bare ``long`` only with a geo-family companion token
        return bool(toks & ({"coord", "gps", "geo", "position"}))
    return bool(toks & _GEO_NAME_TOKENS)


def name_suggests_latitude(col_name: str) -> bool:
    toks = _tokens(col_name)
    blob = (col_name or "").lower()
    if any(ff in blob for ff in _LAT_FALSE_FRIENDS) and not (toks & _LAT_NAME_TOKENS):
        return False
    return bool(toks & _LAT_NAME_TOKENS)


def name_suggests_longitude(col_name: str) -> bool:
    toks = _tokens(col_name)
    blob = (col_name or "").lower()
    if any(ff in blob for ff in _LON_FALSE_FRIENDS):
        return bool(toks & _LON_NAME_TOKENS)
    if toks & _LON_NAME_TOKENS:
        return True
    if toks & _LON_AMBIGUOUS:
        return bool(toks & ({"coord", "gps", "geo", "position"}))
    return False


def _stem(col_name: str) -> str:
    """Strip lat/lon tokens to recover a shared partner stem (cell_lat → cell)."""
    parts = [p for p in _COL_SPLIT.split((col_name or "").lower()) if p]
    geo = _LAT_NAME_TOKENS | _LON_NAME_TOKENS | {"long"}
    kept = [p for p in parts if p not in geo]
    return "_".join(kept)


def find_geo_partners(columns: list[str]) -> dict[str, Optional[str]]:
    """Map each lat/lon-ish column to its complementary partner when detectable."""
    partners: dict[str, Optional[str]] = {c: None for c in columns}
    lat_cols = [c for c in columns if name_suggests_latitude(c)]
    lon_cols = [c for c in columns if name_suggests_longitude(c)]

    # Prefer stem match (cell_lat ↔ cell_lon).
    lon_by_stem = {_stem(c): c for c in lon_cols}
    lat_by_stem = {_stem(c): c for c in lat_cols}
    for lat in lat_cols:
        stem = _stem(lat)
        lon = lon_by_stem.get(stem)
        if lon and lat != lon:
            partners[lat] = lon
            partners[lon] = lat

    # Fallback: unique lat + unique lon in the table.
    unmatched_lat = [c for c in lat_cols if partners.get(c) is None]
    unmatched_lon = [c for c in lon_cols if partners.get(c) is None]
    if len(unmatched_lat) == 1 and len(unmatched_lon) == 1:
        partners[unmatched_lat[0]] = unmatched_lon[0]
        partners[unmatched_lon[0]] = unmatched_lat[0]
    return partners


def _decimal_places(s: str) -> int:
    if "." not in s:
        return 0
    frac = s.split(".", 1)[1]
    frac = re.sub(r"[^\d].*", "", frac)
    return len(frac)


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    if len(s) % 2:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def _parse_float(raw: object) -> float | None:
    try:
        return float(str(raw).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def geo_column_signals(
    values: list,
    *,
    partner_values: Optional[list] = None,
    geofence: str = "egypt",
    min_decimal_places: int = 4,
) -> GeoSignals:
    """Compute Layer-2/3 geo evidence for one column (optionally paired)."""
    checked = 0
    lat_ok = 0
    lon_ok = 0
    out_of_coord = 0
    null_island = 0
    places: list[float] = []
    abs_vals: list[float] = []
    non_null: list[str] = []
    floats: list[float] = []

    for raw in values:
        if not _is_non_null(raw):
            continue
        s = str(raw).strip()
        non_null.append(s)
        checked += 1
        places.append(float(_decimal_places(s)))
        v = _parse_float(s)
        if v is None:
            continue
        floats.append(v)
        abs_vals.append(abs(v))
        if v == 0.0:
            null_island += 1
        if abs(v) > 180:
            out_of_coord += 1
        if -90 <= v <= 90:
            lat_ok += 1
        if -180 <= v <= 180:
            lon_ok += 1

    # Geofence needs pairs.
    egypt_hits = 0
    pair_n = 0
    if partner_values is not None:
        for a, b in zip(values, partner_values):
            if not _is_non_null(a) or not _is_non_null(b):
                continue
            va, vb = _parse_float(a), _parse_float(b)
            if va is None or vb is None:
                continue
            # Assign lat/lon by magnitude when ambiguous.
            if abs(va) <= 90 and abs(vb) <= 180:
                lat, lon = va, vb
            elif abs(vb) <= 90 and abs(va) <= 180:
                lat, lon = vb, va
            else:
                continue
            pair_n += 1
            if in_geofence(geofence, lat, lon):
                egypt_hits += 1

    geofence_rate = (egypt_hits / pair_n) if pair_n else 0.0
    med_places = _median(places)
    abs_max = max(abs_vals) if abs_vals else 0.0
    distinct_rate = (len(set(non_null)) / len(non_null)) if non_null else 0.0

    if checked == 0 or out_of_coord > 0:
        range_signal = "none"
    elif abs_max > 90:
        range_signal = "longitude"
    elif lat_ok == checked and lon_ok == checked:
        range_signal = "both"
    elif lat_ok == checked:
        range_signal = "latitude"
    elif lon_ok == checked:
        range_signal = "longitude"
    else:
        range_signal = "none"

    return GeoSignals(
        name_signal=False,  # filled by caller
        partner_signal=partner_values is not None,
        range_signal=range_signal,
        precision_signal=med_places >= float(min_decimal_places),
        geofence_rate=geofence_rate,
        scaled_integer=None,
        null_island_rate=(null_island / checked) if checked else 0.0,
        decimal_places_median=med_places,
        abs_max=abs_max,
        distinct_rate=distinct_rate,
        lat_in_range_rate=(lat_ok / checked) if checked else 0.0,
        lon_in_range_rate=(lon_ok / checked) if checked else 0.0,
        egypt_geofence_hits=egypt_hits,
        checked=checked,
    )


def detect_coordinate_scale(
    values: list,
    partner_values: list,
    *,
    geofence: str = "egypt",
    geofence_rate_min: float = 0.80,
) -> Optional[int]:
    """Return 1e6 or 1e7 when both columns look like scaled coordinates."""
    for scale in (1_000_000, 10_000_000):
        ok = 0
        checked = 0
        fence_ok = 0
        pairs = 0
        for a, b in zip(values, partner_values):
            if not _is_non_null(a) or not _is_non_null(b):
                continue
            try:
                ia = int(str(a).strip().lstrip("+-"))
                ib = int(str(b).strip().lstrip("+-"))
            except (TypeError, ValueError):
                continue
            # Scaled ints are typically 7–9 digits.
            if not (7 <= len(str(abs(ia))) <= 9 and 7 <= len(str(abs(ib))) <= 9):
                continue
            va, vb = ia / scale, ib / scale
            checked += 1
            if abs(va) <= 90 and abs(vb) <= 180:
                ok += 1
                lat, lon = va, vb
            elif abs(vb) <= 90 and abs(va) <= 180:
                ok += 1
                lat, lon = vb, va
            else:
                continue
            pairs += 1
            if in_geofence(geofence, lat, lon):
                fence_ok += 1
        if checked and (ok / checked) >= 0.95 and pairs and (fence_ok / pairs) >= geofence_rate_min:
            return scale
    return None


def geo_verdict(
    sig: GeoSignals,
    *,
    require_pair: bool = True,
) -> str:
    """Returns ``latitude`` | ``longitude`` | ``both`` | ``none``. Evidence only."""
    if sig.range_signal == "none":
        return "none"
    if require_pair and not sig.partner_signal:
        return "none"
    strong = sum((
        sig.name_signal,
        sig.precision_signal,
        sig.geofence_rate >= 0.80,
        sig.scaled_integer is not None,
    ))
    return sig.range_signal if strong >= 1 else "none"


def geo_confidence(sig: GeoSignals, *, require_pair: bool = True) -> float:
    """Composite 0–1 confidence for the equation gate (not a verdict)."""
    if geo_verdict(sig, require_pair=require_pair) == "none":
        return 0.0
    parts = [
        0.30 if sig.name_signal else 0.0,
        0.30 if sig.partner_signal else 0.0,
        0.25 if sig.precision_signal else 0.0,
        0.15 * min(1.0, sig.geofence_rate),
        0.10 if sig.scaled_integer is not None else 0.0,
    ]
    return round(min(1.0, sum(parts)), 4)
