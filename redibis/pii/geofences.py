"""Named geofences for locale phone/geo packs."""

from __future__ import annotations

from typing import Callable

GeofenceFn = Callable[[float, float], bool]

_EGYPT_LAT = (22.0, 32.0)
_EGYPT_LON = (24.0, 37.0)


def _egypt(lat: float, lon: float) -> bool:
    return _EGYPT_LAT[0] <= lat <= _EGYPT_LAT[1] and _EGYPT_LON[0] <= lon <= _EGYPT_LON[1]


GEOFENCES: dict[str, GeofenceFn] = {
    "egypt": _egypt,
}


def in_geofence(name: str | None, lat: float, lon: float) -> bool:
    if not name:
        return False
    fn = GEOFENCES.get(str(name).strip().lower())
    if fn is None:
        return False
    return fn(lat, lon)
