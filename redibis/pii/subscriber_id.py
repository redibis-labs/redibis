"""IMSI structural validation — MCC registry + home/roaming classification.

Pure module — no Presidio. Stages mirror ``national_id_egypt.NidResult``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional, Sequence

# ITU E.212 Mobile Country Codes (public, stable). Not every historically
# assigned code — enough to accept roamers and reject unassigned prefixes.
KNOWN_MCCS: frozenset[str] = frozenset({
    "202", "204", "206", "208", "212", "213", "214", "216", "218", "219",
    "220", "221", "222", "226", "228", "230", "231", "232", "234", "235",
    "238", "240", "242", "244", "246", "247", "248", "250", "255", "257",
    "259", "260", "262", "266", "268", "270", "272", "274", "276", "278",
    "280", "282", "283", "284", "286", "288", "290", "292", "293", "294",
    "295", "297", "302", "308", "310", "311", "312", "313", "314", "315",
    "316", "330", "332", "334", "338", "340", "342", "344", "346", "348",
    "350", "352", "354", "356", "358", "360", "362", "363", "364", "365",
    "366", "368", "370", "372", "374", "376", "400", "401", "402", "404",
    "405", "406", "410", "412", "413", "414", "415", "416", "417", "418",
    "419", "420", "421", "422", "424", "425", "426", "427", "428", "429",
    "430", "431", "432", "434", "436", "437", "438", "440", "441", "450",
    "452", "454", "455", "456", "457", "460", "461", "466", "467", "470",
    "472", "502", "505", "510", "514", "515", "520", "525", "528", "530",
    "536", "537", "539", "540", "541", "542", "543", "544", "545", "546",
    "547", "548", "549", "550", "551", "552", "553", "554", "555", "602",
    "603", "604", "605", "606", "607", "608", "609", "610", "611", "612",
    "613", "614", "615", "616", "617", "618", "619", "620", "621", "622",
    "623", "624", "625", "626", "627", "628", "629", "630", "631", "632",
    "633", "634", "635", "636", "637", "638", "639", "640", "641", "642",
    "643", "645", "646", "647", "648", "649", "650", "651", "652", "653",
    "654", "655", "657", "658", "659", "702", "704", "706", "708", "710",
    "712", "714", "716", "722", "724", "730", "732", "734", "736", "738",
    "740", "744", "746", "748", "750", "901",
})

_STRIP = frozenset(" \t-_/.\u00a0")
_NULLISH = frozenset({"NONE", "NAN", "N/A", "NULL", ""})


@dataclass(frozen=True)
class ImsiResult:
    valid: bool
    reason: str = ""
    stage_reached: str = ""
    mcc: str = ""
    mnc: str = ""
    msin: str = ""
    is_home: bool = False
    mnc_validated: bool = False

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


def normalize_imsi(raw: object) -> str:
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


def _split_mnc(
    digits: str,
    *,
    roaming_mncs: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, str, str, bool]:
    """Return (mcc, mnc, msin, mnc_validated). Prefer 2-digit MNC unless configured."""
    mcc = digits[:3]
    partners = roaming_mncs or {}
    known = [str(m) for m in partners.get(mcc, ())]
    # Try configured lengths first (2 or 3).
    for mnc_len in (2, 3):
        mnc = digits[3:3 + mnc_len]
        if known and mnc in known:
            return mcc, mnc, digits[3 + mnc_len:], True
    # Default: 2-digit MNC (Egypt / most operators).
    return mcc, digits[3:5], digits[5:], False


def validate_imsi(
    raw: object,
    *,
    home_mcc: str = "602",
    roaming_mncs: Mapping[str, Sequence[str]] | None = None,
) -> ImsiResult:
    """Stages: normalize → length(14..15) → mcc_known → mnc_known → home/roaming."""
    digits = normalize_imsi(raw)
    if not digits:
        return ImsiResult(valid=False, reason="normalize", stage_reached="normalize")
    if len(digits) not in (14, 15) or not digits.isdigit():
        return ImsiResult(valid=False, reason="format", stage_reached="format")

    mcc = digits[:3]
    if mcc not in KNOWN_MCCS:
        return ImsiResult(
            valid=False, reason="mcc_known", stage_reached="mcc_known", mcc=mcc,
        )

    mcc_out, mnc, msin, mnc_ok = _split_mnc(digits, roaming_mncs=roaming_mncs)
    # When roaming partners declare MNCs for this MCC, require membership.
    partners = roaming_mncs or {}
    if mcc in partners and partners[mcc] and not mnc_ok:
        return ImsiResult(
            valid=False, reason="mnc_known", stage_reached="mnc_known",
            mcc=mcc_out, mnc=mnc, msin=msin, mnc_validated=False,
        )

    return ImsiResult(
        valid=True,
        reason="",
        stage_reached="mnc_known" if mnc_ok else "mcc_known",
        mcc=mcc_out,
        mnc=mnc,
        msin=msin,
        is_home=(mcc_out == str(home_mcc)),
        mnc_validated=mnc_ok,
    )


def imsi_rates(
    values: list,
    *,
    home_mcc: str = "602",
    roaming_mncs: Mapping[str, Sequence[str]] | None = None,
) -> dict:
    """Column-level IMSI stats. Denominator = 14/15-digit post-normalize values."""
    checked = valid_n = home_n = 0
    for raw in values:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s or s.upper() in _NULLISH:
            continue
        digits = normalize_imsi(raw)
        if len(digits) not in (14, 15):
            continue
        checked += 1
        res = validate_imsi(digits, home_mcc=home_mcc, roaming_mncs=roaming_mncs)
        if res.valid:
            valid_n += 1
            if res.is_home:
                home_n += 1
    return {
        "imsi_valid_rate": (valid_n / checked) if checked else 0.0,
        "imsi_checked": checked,
        "imsi_valid": valid_n,
        "imsi_home_rate": (home_n / valid_n) if valid_n else 0.0,
    }
