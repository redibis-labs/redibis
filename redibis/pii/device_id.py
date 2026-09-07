"""IMEI / IMEISV structural validation (Luhn + Reporting Body Identifier).

Pure module — no Presidio. Stages mirror ``national_id_egypt.NidResult``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

# GSMA / Type Allocation Code Reporting Body Identifiers (first two TAC digits).
# Not exhaustive of every historical issuer; covers the commonly assigned set.
RBI_CODES: frozenset[str] = frozenset({
    "01", "10", "30", "33", "35", "44", "45", "49",
    "50", "51", "52", "53", "54", "86", "91", "98", "99",
})

_STRIP = frozenset(" \t-_/.\u00a0")
_NULLISH = frozenset({"NONE", "NAN", "N/A", "NULL", ""})


@dataclass(frozen=True)
class ImeiResult:
    valid: bool
    reason: str = ""
    stage_reached: str = ""
    tac: str = ""
    rbi: str = ""
    snr: str = ""
    check_digit: str = ""
    svn: str = ""
    kind: str = ""  # "imei" | "imeisv"
    tac_known: Optional[bool] = None
    luhn_ok: Optional[bool] = None
    rbi_ok: Optional[bool] = None

    def __bool__(self) -> bool:
        return self.valid

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_imei(raw: object) -> str:
    """Digits only. Rejects letters rather than silently dropping them."""
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, float):
        if not raw.is_integer():
            return ""
        raw = int(raw)
    if isinstance(raw, int):
        return str(raw)
    out: list[str] = []
    for ch in str(raw):
        if ch in _STRIP:
            continue
        if ch.isdigit():
            out.append(ch)
        else:
            return ""
    return "".join(out)


def _luhn_ok(digits: str) -> bool:
    nums = [int(d) for d in digits]
    if not nums:
        return False
    checksum = 0
    parity = len(nums) % 2
    for i, d in enumerate(nums):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def validate_imei(
    raw: object,
    *,
    verify_rbi: bool = True,
    tac_known_set: Optional[frozenset[str]] = None,
) -> ImeiResult:
    """Stages: normalize → format(15) → luhn → rbi → (optional) tac_known."""
    digits = normalize_imei(raw)
    if not digits:
        return ImeiResult(valid=False, reason="normalize", stage_reached="normalize", kind="imei")
    if len(digits) != 15 or not digits.isdigit():
        return ImeiResult(valid=False, reason="format", stage_reached="format", kind="imei")

    tac, snr, cd = digits[:8], digits[8:14], digits[14]
    rbi = digits[:2]
    luhn = _luhn_ok(digits)
    if not luhn:
        return ImeiResult(
            valid=False, reason="luhn", stage_reached="luhn",
            tac=tac, rbi=rbi, snr=snr, check_digit=cd, kind="imei", luhn_ok=False,
        )

    rbi_ok = rbi in RBI_CODES
    if verify_rbi and not rbi_ok:
        return ImeiResult(
            valid=False, reason="rbi", stage_reached="rbi",
            tac=tac, rbi=rbi, snr=snr, check_digit=cd, kind="imei",
            luhn_ok=True, rbi_ok=False,
        )

    tac_known: Optional[bool] = None
    if tac_known_set is not None:
        tac_known = tac in tac_known_set
        if not tac_known:
            return ImeiResult(
                valid=False, reason="tac_known", stage_reached="tac_known",
                tac=tac, rbi=rbi, snr=snr, check_digit=cd, kind="imei",
                luhn_ok=True, rbi_ok=rbi_ok, tac_known=False,
            )

    return ImeiResult(
        valid=True, reason="", stage_reached="rbi" if verify_rbi else "luhn",
        tac=tac, rbi=rbi, snr=snr, check_digit=cd, kind="imei",
        luhn_ok=True, rbi_ok=rbi_ok, tac_known=tac_known,
    )


def validate_imeisv(raw: object, *, verify_rbi: bool = True) -> ImeiResult:
    """IMEISV = TAC(8)+SNR(6)+SVN(2). No Luhn — SVN replaces the check digit."""
    digits = normalize_imei(raw)
    if not digits:
        return ImeiResult(valid=False, reason="normalize", stage_reached="normalize", kind="imeisv")
    if len(digits) != 16 or not digits.isdigit():
        return ImeiResult(valid=False, reason="format", stage_reached="format", kind="imeisv")

    tac, snr, svn = digits[:8], digits[8:14], digits[14:16]
    rbi = digits[:2]
    rbi_ok = rbi in RBI_CODES
    if verify_rbi and not rbi_ok:
        return ImeiResult(
            valid=False, reason="rbi", stage_reached="rbi",
            tac=tac, rbi=rbi, snr=snr, svn=svn, kind="imeisv", rbi_ok=False,
        )
    return ImeiResult(
        valid=True, reason="", stage_reached="rbi" if verify_rbi else "format",
        tac=tac, rbi=rbi, snr=snr, svn=svn, kind="imeisv", rbi_ok=rbi_ok,
    )


def imei_rates(
    values: list,
    *,
    verify_rbi: bool = True,
) -> dict:
    """Column-level IMEI stats. Denominator = 15-digit (post-normalize) values."""
    checked = valid_n = 0
    for raw in values:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s or s.upper() in _NULLISH:
            continue
        digits = normalize_imei(raw)
        if len(digits) != 15:
            continue
        checked += 1
        if validate_imei(digits, verify_rbi=verify_rbi):
            valid_n += 1
    return {
        "imei_valid_rate": (valid_n / checked) if checked else 0.0,
        "imei_checked": checked,
        "imei_valid": valid_n,
    }
