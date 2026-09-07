"""Egyptian National ID — value-level validator + column-level rates.

Pure module: ``re``, ``datetime``, ``dataclasses``, ``typing`` only.
Mirrors the phone_engine three-layer pattern (value → column rate → evidence).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_ARABIC_INDIC = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_BIDI_MARKS = frozenset("\u200e\u200f\u061c")
_STRIP_CHARS = frozenset(" \t-_/") | frozenset("\u00a0") | _BIDI_MARKS

# ASCII digits only — ``\d`` would admit Unicode Nd (Devanagari, etc.).
_FORMAT_RE = re.compile(r"[23][0-9]{13}")

GOVERNORATES: dict[str, str] = {
    "01": "Cairo",
    "02": "Alexandria",
    "03": "Port Said",
    "04": "Suez",
    "11": "Damietta",
    "12": "Dakahlia",
    "13": "Sharqia",
    "14": "Qalyubia",
    "15": "Kafr El-Sheikh",
    "16": "Gharbia",
    "17": "Monufia",
    "18": "Beheira",
    "19": "Ismailia",
    "21": "Giza",
    "22": "Beni Suef",
    "23": "Fayoum",
    "24": "Minya",
    "25": "Asyut",
    "26": "Sohag",
    "27": "Qena",
    "28": "Aswan",
    "29": "Luxor",
    "31": "Red Sea",
    "32": "New Valley",
    "33": "Matrouh",
    "34": "North Sinai",
    "35": "South Sinai",
    "88": "Born abroad",
}
_GOV_CODES = frozenset(GOVERNORATES)

_CHECK_WEIGHTS = (2, 7, 6, 5, 4, 3, 2, 7, 6, 5, 4, 3, 2)

_NID_NAME_TOKENS_LATIN = frozenset({
    "nid", "nationalid", "national_id", "ssn", "natid",
})
_NID_NAME_TOKENS_ARABIC = frozenset({
    "رقم_قومي", "الرقم_القومي", "هوية",
})
_NID_NAME_TOKENS = _NID_NAME_TOKENS_LATIN | _NID_NAME_TOKENS_ARABIC

# ``at`` (not ``_at``): the ``(?:^|_)`` prefix already supplies the underscore,
# so ``_at`` would only match a literal ``__at``.
_NOT_NID_NAME_RE = re.compile(
    r"(?:^|_)(ts|time|timestamp|epoch|datetime|dt|date|created|updated|"
    r"modified|inserted|loaded|event_time|at)(?:$|_)",
    re.IGNORECASE,
)

_NULLISH = frozenset({"NONE", "NAN", "N/A", "NULL", ""})

_NID_ENTITY_TYPES = frozenset({"EG_NATIONAL_ID"})


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NidResult:
    """Value-level Egyptian National ID validation outcome.

    ``stage_reached`` is the stage that *failed* when ``valid`` is False, and
    the last stage that *ran* when ``valid`` is True. Use ``valid`` to
    disambiguate; do not treat the field alone as a failure marker.
    """

    valid: bool
    reason: str = ""
    stage_reached: str = ""
    birth_date: Optional[str] = None
    gov_code: str = ""
    governorate: str = ""
    gender: str = ""
    sequence: str = ""
    check_digit: str = ""
    check_digit_ok: Optional[bool] = None

    def __bool__(self) -> bool:
        return self.valid

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def as_dict(self) -> dict:
        """Back-compat dict for callers that index ``result["valid"]``."""
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 0 — normalize
# ─────────────────────────────────────────────────────────────────────────────

def normalize_nid(raw: object) -> str:
    """Digits only, Arabic-Indic folded. Returns ``''`` when nothing usable."""
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, float):
        if not raw.is_integer():
            return ""
        raw = int(raw)
    if isinstance(raw, int):
        s = str(raw)
    else:
        s = str(raw)

    s = s.translate(_ARABIC_INDIC)
    out: list[str] = []
    for ch in s:
        if ch in _STRIP_CHARS:
            continue
        if ch.isdigit():
            out.append(ch)
        else:
            # Non-digit surviving after strip → reject (do not silently drop letters)
            return ""
    return "".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5 — check digit (default off)
# ─────────────────────────────────────────────────────────────────────────────

def check_digit_mod11(nid13: str) -> int:
    """Weighted mod-11 check digit over the first 13 digits.

    Circulating variants disagree on remainders 0/1; this uses the common
    ``(11 - rem) % 11`` form clamped to a single digit (10 → 0). Keep
    ``verify_check_digit=False`` until calibration shows ≥ 0.98 pass rate.
    """
    if len(nid13) != 13 or not nid13.isdigit():
        raise ValueError("nid13 must be exactly 13 digits")
    total = sum(int(d) * w for d, w in zip(nid13, _CHECK_WEIGHTS))
    rem = total % 11
    check = (11 - rem) % 11
    return 0 if check == 10 else check


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — plausibility
# ─────────────────────────────────────────────────────────────────────────────

def _plausible(
    birth: date,
    *,
    today: date,
    max_age: int = 110,
    min_birth_year: int = 1900,
) -> bool:
    if birth.year < min_birth_year:
        return False
    if birth > today:
        return False
    return (today - birth).days <= max_age * 366


# ─────────────────────────────────────────────────────────────────────────────
# Full validator
# ─────────────────────────────────────────────────────────────────────────────

def validate_egypt_national_id(
    raw: object,
    *,
    today: date | None = None,
    max_age: int = 110,
    min_birth_year: int = 1900,
    verify_check_digit: bool = False,
) -> NidResult:
    """Validate an Egyptian National ID through ordered structural stages."""
    today = today or date.today()

    digits = normalize_nid(raw)
    if not digits:
        return NidResult(valid=False, reason="normalize", stage_reached="normalize")

    if _FORMAT_RE.fullmatch(digits) is None:
        return NidResult(valid=False, reason="format", stage_reached="format")

    century = {"2": 1900, "3": 2000}[digits[0]]
    year = century + int(digits[1:3])
    month = int(digits[3:5])
    day = int(digits[5:7])
    gov_code = digits[7:9]
    sequence = digits[9:13]
    check_digit = digits[13]
    gender = "male" if int(digits[12]) % 2 == 1 else "female"

    try:
        birth = date(year, month, day)
    except ValueError:
        return NidResult(
            valid=False,
            reason="calendar",
            stage_reached="calendar",
            gov_code=gov_code,
            gender=gender,
            sequence=sequence,
            check_digit=check_digit,
        )

    birth_iso = birth.isoformat()

    if gov_code not in _GOV_CODES:
        return NidResult(
            valid=False,
            reason="governorate",
            stage_reached="governorate",
            birth_date=birth_iso,
            gov_code=gov_code,
            gender=gender,
            sequence=sequence,
            check_digit=check_digit,
        )

    if not _plausible(
        birth, today=today, max_age=max_age, min_birth_year=min_birth_year,
    ):
        return NidResult(
            valid=False,
            reason="plausibility",
            stage_reached="plausibility",
            birth_date=birth_iso,
            gov_code=gov_code,
            governorate=GOVERNORATES[gov_code],
            gender=gender,
            sequence=sequence,
            check_digit=check_digit,
        )

    try:
        expected = check_digit_mod11(digits[:13])
        check_ok = int(check_digit) == expected
    except (ValueError, TypeError):
        check_ok = False

    if verify_check_digit and not check_ok:
        return NidResult(
            valid=False,
            reason="checkdigit",
            stage_reached="checkdigit",
            birth_date=birth_iso,
            gov_code=gov_code,
            governorate=GOVERNORATES[gov_code],
            gender=gender,
            sequence=sequence,
            check_digit=check_digit,
            check_digit_ok=False,
        )

    return NidResult(
        valid=True,
        reason="",
        stage_reached="checkdigit" if verify_check_digit else "plausibility",
        birth_date=birth_iso,
        gov_code=gov_code,
        governorate=GOVERNORATES[gov_code],
        gender=gender,
        sequence=sequence,
        check_digit=check_digit,
        check_digit_ok=check_ok,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — column name signals + rates
# ─────────────────────────────────────────────────────────────────────────────

def name_suggests_timestamp(
    col_name: str,
    *,
    extra_patterns: list[str] | None = None,
) -> bool:
    """True for time_ts / event_time / created_at — never a national ID by name."""
    name = col_name or ""
    if _NOT_NID_NAME_RE.search(name):
        return True
    for pat in extra_patterns or ():
        try:
            if re.search(pat, name, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def name_has_nid_token(
    col_name: str,
    *,
    extra_patterns: list[str] | None = None,
) -> bool:
    """Positive name signal; False when ``name_suggests_timestamp()`` holds.

    Latin tokens match on underscore/start/end boundaries only (avoids
    ``unidentified`` / ``is_unidentified`` substring hits). Arabic tokens
    also allow a substring fallback because non-letter splitting does not
    segment Arabic script reliably.
    """
    if name_suggests_timestamp(col_name, extra_patterns=extra_patterns):
        return False
    blob = (col_name or "").lower()
    for tok in _NID_NAME_TOKENS_LATIN:
        if re.search(rf"(?:^|_){re.escape(tok)}(?:$|_)", blob):
            return True
    tokens = set(re.split(r"[^a-z0-9_\u0600-\u06ff]+", blob))
    if tokens & _NID_NAME_TOKENS_ARABIC:
        return True
    return any(t in blob for t in _NID_NAME_TOKENS_ARABIC)


def _is_non_null(val: object) -> bool:
    if val is None:
        return False
    s = str(val).strip()
    return bool(s) and s.upper() not in _NULLISH


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def nid_rates(
    values: list,
    *,
    today: date | None = None,
    max_age: int = 110,
    min_birth_year: int = 1900,
    verify_check_digit: bool = False,
) -> dict:
    """Column-level NID stats. Denominator = values surviving Stage 0+1."""
    today = today or date.today()
    checked = 0
    valid_n = 0
    checkdigit_ok_n = 0
    checkdigit_n = 0
    ages: list[float] = []
    govs: set[str] = set()
    format_ok_raw: list[str] = []

    for raw in values:
        if not _is_non_null(raw):
            continue
        digits = normalize_nid(raw)
        if not digits or _FORMAT_RE.fullmatch(digits) is None:
            continue
        checked += 1
        format_ok_raw.append(digits)
        result = validate_egypt_national_id(
            digits,
            today=today,
            max_age=max_age,
            min_birth_year=min_birth_year,
            verify_check_digit=verify_check_digit,
        )
        if result.check_digit_ok is not None:
            checkdigit_n += 1
            if result.check_digit_ok:
                checkdigit_ok_n += 1
        if result.valid:
            valid_n += 1
            if result.birth_date:
                try:
                    birth = date.fromisoformat(result.birth_date)
                    ages.append((today - birth).days / 365.25)
                except ValueError:
                    pass
            if result.gov_code:
                govs.add(result.gov_code)

    # Monotonicity over format-ok numeric strings (Layer 3 evidence).
    mono_rate = 0.0
    if len(format_ok_raw) >= 2:
        mono_hits = 0
        pairs = 0
        for a, b in zip(format_ok_raw, format_ok_raw[1:]):
            pairs += 1
            if a <= b:
                mono_hits += 1
        mono_rate = mono_hits / pairs if pairs else 0.0

    # Same denominator as nid_valid_rate / ages (Stage 0+1 survivors).
    unique_rate: float | None = None
    if format_ok_raw:
        unique_rate = len(set(format_ok_raw)) / len(format_ok_raw)

    ages_sorted = sorted(ages)
    return {
        "nid_valid_rate": (valid_n / checked) if checked else 0.0,
        "nid_checked": checked,
        "nid_valid": valid_n,
        "nid_checkdigit_rate": (
            (checkdigit_ok_n / checkdigit_n) if checkdigit_n else None
        ),
        "nid_age_p05": _percentile(ages_sorted, 0.05),
        "nid_age_p95": _percentile(ages_sorted, 0.95),
        "nid_gov_distinct": len(govs),
        "nid_monotonic_rate": mono_rate,
        "nid_unique_rate": unique_rate,
    }


def is_nid_entity(entity_type: str | None) -> bool:
    """True when *entity_type* is the Egyptian national-ID label."""
    if not entity_type:
        return False
    return entity_type.upper().replace(" ", "_") in _NID_ENTITY_TYPES


_FUNNEL_STAGES = (
    "normalize", "format", "calendar", "governorate", "plausibility", "checkdigit",
)


def calibrate_nid_funnel(
    values: list,
    *,
    today: date | None = None,
    max_age: int = 110,
    min_birth_year: int = 1900,
) -> dict:
    """Per-stage pass counts for the ``calibrate-nid`` CLI command.

    Derived from ``validate_egypt_national_id`` via ``stage_reached`` / ``valid``
    so the funnel cannot drift from the validator. Check-digit is counted among
    values that reached plausibility (``check_digit_ok``), matching the CLI's
    informational check-digit row.
    """
    today = today or date.today()
    counts = {s: 0 for s in _FUNNEL_STAGES}
    non_null = 0

    for raw in values:
        if not _is_non_null(raw):
            continue
        non_null += 1
        result = validate_egypt_national_id(
            raw,
            today=today,
            max_age=max_age,
            min_birth_year=min_birth_year,
            verify_check_digit=False,
        )
        failed = result.stage_reached
        if result.valid:
            # Passed through plausibility; check digit is observational.
            for s in ("normalize", "format", "calendar", "governorate", "plausibility"):
                counts[s] += 1
            if result.check_digit_ok:
                counts["checkdigit"] += 1
            continue
        if failed not in _FUNNEL_STAGES:
            continue
        # stage_reached on failure = first stage that did not pass.
        idx = _FUNNEL_STAGES.index(failed)
        for s in _FUNNEL_STAGES[:idx]:
            counts[s] += 1

    return {"non_null": non_null, "stages": counts}
