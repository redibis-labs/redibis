"""
redibis.masking.transforms — value-level de-identification primitives.

Pure, dependency-light building blocks used by the masking engine:

    - ``KeyedRandom``    deterministic HMAC-DRBG PRNG (joins survive within a run)
    - strategy fns       passthrough / redact / mask / hash / encrypt / fpe / fake
    - locale fakers      English + Arabic names, addresses, phones, emails, …
    - FF3-1 FPE          NIST format-preserving encryption (legacy keystream retained)
    - AES-GCM encrypt    reversible by key-holder (via ``cryptography`` if present)

The Faker library is used opportunistically for richer locale data when it is
installed; otherwise embedded pools keep everything working offline.

Nothing here touches storage, sessions, or PII detection — it only knows a
value, a column name, and a per-run key. Determinism: every deterministic
transform derives its randomness from ``HMAC(run_key + key_ref, column|value)``
so the same input always maps to the same output *within a run*.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import re
import struct
from typing import Optional

log = logging.getLogger(__name__)

# Optional Faker — used when available, embedded pools otherwise.
try:  # pragma: no cover - env dependent
    from faker import Faker as _Faker
    _HAS_FAKER = True
except Exception:  # pragma: no cover
    _Faker = None
    _HAS_FAKER = False

# Optional AES-GCM (reversible encrypt). Falls back to an HMAC keystream cipher.
try:  # pragma: no cover - env dependent
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AESGCM = True
except Exception:  # pragma: no cover
    AESGCM = None
    _HAS_AESGCM = False

# Optional rstr — generates random strings from regex patterns.
try:  # pragma: no cover - env dependent
    import rstr as _rstr
    _HAS_RSTR = True
except Exception:  # pragma: no cover
    _rstr = None
    _HAS_RSTR = False

# Optional FF3-1 format-preserving encryption (NIST SP 800-38G).
try:  # pragma: no cover - env dependent
    from ff3 import FF3Cipher as _FF3Cipher
    _HAS_FF3 = True
except Exception:  # pragma: no cover
    _FF3Cipher = None
    _HAS_FF3 = False


_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")


def is_arabic(text: str) -> bool:
    """True if the string contains Arabic script characters."""
    return bool(text) and bool(_ARABIC_RE.search(str(text)))


def canonicalize_faker_locale(locale: Optional[str]) -> str:
    """Normalize locale aliases used across config, API, CLI, and UI."""
    loc = (locale or "default").strip().lower()
    if loc in ("", "default", "mixed", "auto", "faker_default"):
        return "default"
    if loc in ("ar", "arabic", "faker_arabic", "ar_aa"):
        return "ar"
    if loc in ("en", "english", "faker_english", "en_us"):
        return "en"
    return "default"


def effective_faker_locale(locale: Optional[str],
                           plan_default_locale: Optional[str] = "default") -> str:
    """Resolve a rule-level locale, falling back to the plan default."""
    loc = canonicalize_faker_locale(locale)
    if loc == "default":
        return canonicalize_faker_locale(plan_default_locale)
    return loc


def resolve_locale(value: str, locale: str) -> str:
    """Resolve an effective locale for one value (handles ``mixed``)."""
    loc = canonicalize_faker_locale(locale)
    if loc == "default":
        return "ar" if is_arabic(value) else "en"
    if loc == "ar":
        return "ar"
    return "en"


# ─────────────────────────────────────────────────────────────────────────────
# Keyed deterministic PRNG (HMAC-DRBG style)
# ─────────────────────────────────────────────────────────────────────────────

class KeyedRandom:
    """A deterministic PRNG seeded by a key + label. Stable across processes."""

    def __init__(self, key: bytes, label: str = ""):
        self._key = key
        self._counter = 0
        self._buf = b""
        self._label = label.encode("utf-8")

    def _more(self) -> None:
        block = hmac.new(self._key, self._label + struct.pack(">Q", self._counter),
                         hashlib.sha256).digest()
        self._counter += 1
        self._buf += block

    def bytes(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._more()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def randint(self, lo: int, hi: int) -> int:
        """Uniform int in [lo, hi] (inclusive), low modulo-bias for our ranges."""
        if hi <= lo:
            return lo
        span = hi - lo + 1
        raw = int.from_bytes(self.bytes(8), "big")
        return lo + (raw % span)

    def choice(self, seq):
        return seq[self.randint(0, len(seq) - 1)]

    def digits(self, n: int) -> str:
        return "".join(str(self.randint(0, 9)) for _ in range(n))


def _derive_key(run_key: bytes, key_ref: str) -> bytes:
    return hmac.new(run_key, f"keyref:{key_ref}".encode("utf-8"), hashlib.sha256).digest()


def _value_rng(run_key: bytes, key_ref: str, column: str, value: str,
               deterministic: bool) -> KeyedRandom:
    """Build a per-value PRNG. Deterministic→seeded by value; random→by nonce."""
    sub = _derive_key(run_key, key_ref)
    if deterministic:
        label = f"{column}|{value}"
    else:
        import os
        label = f"{column}|{os.urandom(16).hex()}"
    return KeyedRandom(sub, label)


# ─────────────────────────────────────────────────────────────────────────────
# Embedded locale pools (used when Faker is unavailable)
# ─────────────────────────────────────────────────────────────────────────────

_EN_FIRST_M = ["James", "Liam", "Noah", "William", "Lucas", "Henry", "Jack", "Owen", "Leo", "Daniel"]
_EN_FIRST_F = ["Emma", "Olivia", "Ava", "Sophia", "Isabella", "Mia", "Charlotte", "Amelia", "Grace", "Chloe"]
_EN_LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Wilson", "Taylor", "Davies", "Evans", "Walker"]
_AR_FIRST_M = ["محمد", "أحمد", "محمود", "علي", "حسن", "عمر", "خالد", "يوسف", "إبراهيم", "مصطفى"]
_AR_FIRST_F = ["فاطمة", "عائشة", "مريم", "سارة", "نور", "هدى", "ليلى", "أمل", "ياسمين", "رنا"]
_AR_LAST = ["الهاشمي", "العلي", "المصري", "السيد", "عبدالله", "الشريف", "النجار", "الحسيني", "القاضي", "الفارسي"]
_EN_CITY = ["Springfield", "Riverton", "Fairview", "Greenville", "Madison", "Clinton", "Franklin", "Salem"]
_EN_STREET = ["Main St", "Oak Ave", "Park Rd", "Maple Dr", "Cedar Ln", "Hill St", "Lake Rd", "Elm St"]
_AR_CITY = ["القاهرة", "الإسكندرية", "الجيزة", "الرياض", "جدة", "المنصورة", "أسيوط", "طنطا"]
_AR_STREET = ["شارع النيل", "شارع التحرير", "شارع الجمهورية", "شارع الهرم", "شارع فيصل", "شارع الجامعة"]
_EN_COMPANY = ["Acme Corp", "Globex", "Initech", "Umbrella LLC", "Soylent Inc", "Hooli", "Vandelay", "Stark Industries"]
_AR_COMPANY = ["شركة النور", "مجموعة الأمل", "مؤسسة الرواد", "شركة المستقبل", "مجموعة الفجر", "شركة الإبداع"]
_EMAIL_DOMAINS = ["example.com", "example.net", "example.org", "mail.test", "demo.io"]


def _faker_for(locale: str):
    if not _HAS_FAKER:
        return None
    code = "ar_AA" if locale == "ar" else "en_US"
    try:  # pragma: no cover - env dependent
        f = _Faker(code)
        return f
    except Exception:  # pragma: no cover
        try:
            return _Faker("en_US")
        except Exception:
            return None


def _seeded_faker(rng: KeyedRandom, locale: str):
    """Create a deterministically seeded Faker instance for one value."""
    faker = _faker_for(locale)
    if faker is None:
        return None
    seed = int.from_bytes(rng.bytes(8), "big")
    try:  # pragma: no cover - env dependent
        faker.seed_instance(seed)
    except Exception:
        try:
            faker.random.seed(seed)
        except Exception:
            return None
    return faker


def _faker_text(faker, *methods: str) -> Optional[str]:
    """Call the first available Faker provider and normalize newlines."""
    for method in methods:
        provider = getattr(faker, method, None)
        if not callable(provider):
            continue
        try:  # pragma: no cover - env dependent
            value = provider()
        except Exception:
            continue
        if value:
            return re.sub(r"\s+", " ", str(value).replace("\n", ", ")).strip(" ,")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Fakers (locale-aware)
# ─────────────────────────────────────────────────────────────────────────────

def fake_name(rng: KeyedRandom, locale: str, gender: Optional[str] = None) -> str:
    g = gender or ("f" if rng.randint(0, 1) else "m")
    faker = _seeded_faker(rng, locale)
    if faker is not None:
        name = _faker_text(faker, "name_female" if g == "f" else "name_male", "name")
        if name:
            return name
    if locale == "ar":
        first = rng.choice(_AR_FIRST_F if g == "f" else _AR_FIRST_M)
        return f"{first} {rng.choice(_AR_LAST)}"
    first = rng.choice(_EN_FIRST_F if g == "f" else _EN_FIRST_M)
    return f"{first} {rng.choice(_EN_LAST)}"


def fake_address(rng: KeyedRandom, locale: str) -> str:
    faker = _seeded_faker(rng, locale)
    if faker is not None:
        address = _faker_text(faker, "address")
        if address:
            return address
    num = rng.randint(1, 299)
    if locale == "ar":
        return f"{rng.choice(_AR_STREET)} {num}، {rng.choice(_AR_CITY)}"
    return f"{num} {rng.choice(_EN_STREET)}, {rng.choice(_EN_CITY)}"


def fake_company(rng: KeyedRandom, locale: str) -> str:
    faker = _seeded_faker(rng, locale)
    if faker is not None:
        company = _faker_text(faker, "company")
        if company:
            return company
    return rng.choice(_AR_COMPANY if locale == "ar" else _EN_COMPANY)


def fake_phone(rng: KeyedRandom, original: str, *, preserve_format: bool = True,
               preserve_country_code: bool = True, region: str = "") -> str:
    """Randomize digits while keeping the original grouping/length/country code."""
    s = str(original)
    if not preserve_format or not any(ch.isdigit() for ch in s):
        cc = "+20" if (region or "").upper() == "EG" else "+1"
        return f"{cc} {rng.digits(3)} {rng.digits(3)} {rng.digits(4)}"
    # Walk the string; replace digits. Optionally keep a leading +<cc> intact.
    out = list(s)
    digit_positions = [i for i, ch in enumerate(s) if ch.isdigit()]
    keep = 0
    if preserve_country_code and s.lstrip().startswith("+"):
        # keep digits that belong to the country code (up to the first space/sep)
        m = re.match(r"\s*\+\d{1,3}", s)
        if m:
            keep = sum(1 for ch in m.group(0) if ch.isdigit())
    for n, i in enumerate(digit_positions):
        if n < keep:
            continue
        out[i] = str(rng.randint(0, 9))
    return "".join(out)


def fake_email(rng: KeyedRandom, original: str, *, preserve_domain: bool = True,
               local_part: Optional[str] = None) -> str:
    """Fake an email. local_part (e.g. from a faked name) used when provided."""
    s = str(original)
    domain = ""
    if "@" in s:
        domain = s.split("@", 1)[1]
    if local_part:
        lp = re.sub(r"[^a-z0-9._]+", "", local_part.lower().replace(" ", "."))
        lp = lp.strip(".") or f"user{rng.randint(10, 9999)}"
    else:
        lp = f"{rng.choice(['user', 'rand', 'anon', 'cust'])}{rng.randint(10, 99999)}"
    if preserve_domain and domain:
        return f"{lp}@{domain}"
    return f"{lp}@{rng.choice(_EMAIL_DOMAINS)}"


def fake_national_id(rng: KeyedRandom, original: str) -> str:
    n = sum(1 for ch in str(original) if ch.isdigit()) or 14
    return rng.digits(n)


def fake_credit_card(rng: KeyedRandom, original: str) -> str:
    """Format-preserving random card-like number (not Luhn-valid by design)."""
    return _preserve_digit_shape(str(original), rng)


def fake_date(rng: KeyedRandom, original: str, *, jitter_days: int = 30) -> str:
    """Jitter an ISO date within ±jitter_days; pass through if unparseable."""
    from datetime import datetime, timedelta
    s = str(original)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s[:10], fmt)
            delta = rng.randint(-jitter_days, jitter_days)
            return (d + timedelta(days=delta)).strftime(fmt)
        except ValueError:
            continue
    return s


def fake_uuid(rng: KeyedRandom) -> str:
    b = rng.bytes(16)
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _preserve_digit_shape(s: str, rng: KeyedRandom) -> str:
    return "".join(str(rng.randint(0, 9)) if ch.isdigit() else ch for ch in s)


# ─────────────────────────────────────────────────────────────────────────────
# Format-preserving encryption (FF3-1 default; legacy keystream retained)
# ─────────────────────────────────────────────────────────────────────────────

_ALPHABETS = {
    "digits": "0123456789",
    "alnum": "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
}

_FPE_ALGORITHMS = {
    "ff3": "FF3-1 AES-256",
    "keystream": "HMAC keystream (legacy)",
}


def _alpha_string(alphabet: str) -> str:
    return _ALPHABETS.get(alphabet, alphabet)


def _fpe_domain_limits(alphabet: str) -> tuple[int, int, int]:
    """Return (radix, min_len, max_len) for an alphabet."""
    alpha = _alpha_string(alphabet)
    radix = len(alpha)
    if radix < 2:
        return radix, 0, 0
    min_len = math.ceil(math.log(1_000_000, radix))
    max_len = int(2 * math.floor(96 / math.log2(radix)))
    return radix, min_len, max_len


def _derive_fpe_tweak(tweak: str, column: str = "") -> str:
    """FF3-1 requires a 7-byte (56-bit) tweak — deterministic per column."""
    label = tweak or column or ""
    return hashlib.sha256(("redibis-fpe:" + label).encode("utf-8")).digest()[:7].hex()


def default_fpe_mode() -> str:
    """Preferred FPE mode for new rules: FF3-1 when installed, else legacy keystream."""
    return "ff3" if _HAS_FF3 else "keystream"


def resolve_fpe_mode(mode: Optional[str], *, column: str = "",
                     require_ff3: bool = False) -> str:
    """Resolve effective FPE mode: explicit mode → ff3 if available → keystream."""
    if mode == "keystream":
        return "keystream"
    if mode == "ff3":
        if not _HAS_FF3:
            if require_ff3:
                raise ValueError(
                    f"FPE column {column!r}: mode ff3 required but ff3 is not installed "
                    "(pip install redibis[mask])"
                )
            log.warning(
                "FPE column %r: mode ff3 requested but ff3 not installed; "
                "falling back to legacy keystream",
                column,
            )
            return "keystream"
        return "ff3"
    if _HAS_FF3:
        return "ff3"
    if require_ff3:
        raise ValueError(
            f"FPE column {column!r}: authenticated FPE (ff3) required but ff3 is not "
            "installed (pip install redibis[mask])"
        )
    log.warning(
        "FPE column %r: ff3 not installed; using legacy keystream mode. "
        "Install ff3 or set mode: keystream explicitly.",
        column,
    )
    return "keystream"


def _keystream_shift(key: bytes, tweak: str, pos: int, radix: int) -> int:
    raw = hmac.new(key, f"{tweak}:{pos}".encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(raw[:8], "big") % radix


def _fpe_keystream_transform(value: str, key: bytes, *, alphabet: str = "digits",
                             tweak: str = "", decrypt: bool = False) -> str:
    alpha = _alpha_string(alphabet)
    if not alpha:
        return value
    radix = len(alpha)
    index = {c: i for i, c in enumerate(alpha)}
    out = []
    pos = 0
    for ch in str(value):
        if ch in index:
            shift = _keystream_shift(key, tweak, pos, radix)
            i = index[ch]
            j = (i - shift) % radix if decrypt else (i + shift) % radix
            out.append(alpha[j])
            pos += 1
        else:
            out.append(ch)
    return "".join(out)


def _extract_alpha_chars(value: str, alphabet: str) -> tuple[str, list[int]]:
    """Return concatenated in-alphabet chars and their positions."""
    alpha = _alpha_string(alphabet)
    index = set(alpha)
    chars: list[str] = []
    positions: list[int] = []
    for i, ch in enumerate(str(value)):
        if ch in index:
            chars.append(ch)
            positions.append(i)
    return "".join(chars), positions


def _reinsert_alpha_chars(value: str, transformed: str, positions: list[int]) -> str:
    out = list(str(value))
    for pos, ch in zip(positions, transformed):
        out[pos] = ch
    return "".join(out)


def _ff3_cipher(key: bytes, tweak_hex: str, alphabet: str):
    key_hex = key[:32].hex()
    if alphabet in _ALPHABETS:
        alpha = _ALPHABETS[alphabet]
        return _FF3Cipher(key_hex, tweak_hex, radix=len(alpha))
    return _FF3Cipher.withCustomAlphabet(key_hex, tweak_hex, alphabet)


def _fpe_ff3_transform(value: str, key: bytes, *, alphabet: str = "digits",
                       tweak: str = "", decrypt: bool = False,
                       short_value_policy: str = "error",
                       column: str = "",
                       meta_out: Optional[dict] = None) -> str:
    alpha = _alpha_string(alphabet)
    if not alpha:
        return value
    core, positions = _extract_alpha_chars(value, alphabet)
    n = len(core)
    if n == 0:
        return value

    tweak_hex = _derive_fpe_tweak(tweak, column)
    _, min_len, max_len = _fpe_domain_limits(alphabet)

    if n > max_len:
        raise ValueError(
            f"FPE column {column!r}: value has {n} in-alphabet characters "
            f"(max {max_len} for alphabet radix {len(alpha)})"
        )
    if n < min_len:
        if short_value_policy == "keystream":
            if meta_out is not None:
                meta_out.setdefault("modes_used", set()).add("keystream")
            return _fpe_keystream_transform(
                value, key, alphabet=alphabet, tweak=tweak, decrypt=decrypt,
            )
        if short_value_policy == "passthrough":
            if meta_out is not None:
                meta_out.setdefault("modes_used", set()).add("passthrough")
            return value
        raise ValueError(
            f"FPE column {column!r}: value has {n} in-alphabet characters "
            f"(minimum {min_len} for FF3-1 with radix {len(alpha)})"
        )

    cipher = _ff3_cipher(key, tweak_hex, alphabet)
    transformed_core = cipher.decrypt(core) if decrypt else cipher.encrypt(core)
    if meta_out is not None:
        meta_out.setdefault("modes_used", set()).add("ff3")
    return _reinsert_alpha_chars(value, transformed_core, positions)


def fpe_transform(value: str, key: bytes, *, alphabet: str = "digits",
                  tweak: str = "", decrypt: bool = False,
                  mode: Optional[str] = None,
                  short_value_policy: str = "error",
                  column: str = "",
                  require_ff3: bool = False,
                  meta_out: Optional[dict] = None) -> str:
    """Reversible, format-preserving transform over a fixed alphabet.

    Characters outside the alphabet pass through unchanged (separators preserved).
    Default mode is FF3-1 when ``ff3`` is installed; legacy ``keystream`` remains
    available for back-compat and short-value fallback.
    """
    effective = resolve_fpe_mode(mode, column=column, require_ff3=require_ff3)
    if effective == "ff3":
        return _fpe_ff3_transform(
            value, key, alphabet=alphabet, tweak=tweak, decrypt=decrypt,
            short_value_policy=short_value_policy, column=column, meta_out=meta_out,
        )
    if meta_out is not None:
        meta_out.setdefault("modes_used", set()).add("keystream")
    return _fpe_keystream_transform(
        value, key, alphabet=alphabet, tweak=tweak, decrypt=decrypt,
    )


def fpe_algorithm_label(mode: str, modes_used: Optional[set] = None) -> str:
    if modes_used and len(modes_used) > 1:
        return "mixed (FF3-1 + keystream)"
    if modes_used and "passthrough" in modes_used:
        return "mixed (FF3-1 + passthrough)"
    return _FPE_ALGORITHMS.get(mode, mode)


# ─────────────────────────────────────────────────────────────────────────────
# Reversible encryption
# ─────────────────────────────────────────────────────────────────────────────

def encrypt_value(value: str, key: bytes, *, require_aes_gcm: bool = False) -> str:
    """AES-256-GCM (if available) else HMAC keystream. Output: 'g1:' / 'x1:'."""
    data = str(value).encode("utf-8")
    if _HAS_AESGCM:
        import os
        nonce = os.urandom(12)
        ct = AESGCM(key[:32]).encrypt(nonce, data, None)
        return "g1:" + base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    if require_aes_gcm:
        raise ValueError(
            "encrypt strategy requires AES-256-GCM but cryptography is not installed "
            "(pip install redibis[mask])"
        )
    ks = KeyedRandom(key, "stream").bytes(len(data))
    xored = bytes(a ^ b for a, b in zip(data, ks))
    return "x1:" + base64.urlsafe_b64encode(xored).decode("ascii")


def decrypt_value(token: str, key: bytes) -> str:
    s = str(token)
    if s.startswith("g1:") and _HAS_AESGCM:
        raw = base64.urlsafe_b64decode(s[3:])
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(key[:32]).decrypt(nonce, ct, None).decode("utf-8")
    if s.startswith("x1:"):
        xored = base64.urlsafe_b64decode(s[3:])
        ks = KeyedRandom(key, "stream").bytes(len(xored))
        return bytes(a ^ b for a, b in zip(xored, ks)).decode("utf-8")
    raise ValueError("Unrecognized ciphertext token")


# ─────────────────────────────────────────────────────────────────────────────
# Non-reversible primitives
# ─────────────────────────────────────────────────────────────────────────────

def hash_value(value: str, *, algo: str = "sha256", hmac_key: Optional[bytes] = None,
               truncate: int = 0, prefix: str = "") -> str:
    data = str(value).encode("utf-8")
    if hmac_key:
        digest = hmac.new(hmac_key, data, getattr(hashlib, algo, hashlib.sha256)).hexdigest()
    else:
        digest = getattr(hashlib, algo, hashlib.sha256)(data).hexdigest()
    if truncate and truncate > 0:
        digest = digest[:truncate]
    return f"{prefix}{digest}"


def mask_value(value: str, *, keep_first: int = 0, keep_last: int = 4,
               mask_char: str = "*", preserve_length: bool = True) -> str:
    s = str(value)
    n = len(s)
    if n == 0:
        return s
    kf = max(0, min(keep_first, n))
    kl = max(0, min(keep_last, n - kf))
    hidden = n - kf - kl
    if hidden <= 0:
        return s
    middle = mask_char * (hidden if preserve_length else min(hidden, 4))
    return s[:kf] + middle + (s[n - kl:] if kl else "")


# ─────────────────────────────────────────────────────────────────────────────
# Position-based slicing (apply a transform to a substring)
# ─────────────────────────────────────────────────────────────────────────────

def apply_position_slice(value: str, transform_fn, *,
                         start_index: int = 0,
                         end_index: Optional[int] = None) -> str:
    """Apply *transform_fn* to a substring ``value[start_index:end_index]``.

    Characters outside the slice are preserved verbatim.

    Parameters
    ----------
    value : str
        The full original value.
    transform_fn : callable(str) -> str
        A function that transforms a string (e.g. ``mask_value``, ``fpe_transform``).
    start_index : int
        0-based inclusive start position (default 0 = beginning).
    end_index : int | None
        0-based exclusive end position.  ``None`` means end of string.
    """
    s = str(value)
    si = max(0, start_index)
    ei = end_index if end_index is not None else len(s)
    ei = min(ei, len(s))
    if si >= ei:
        return s  # nothing to transform
    prefix = s[:si]
    target = s[si:ei]
    suffix = s[ei:]
    return prefix + transform_fn(target) + suffix


# ─────────────────────────────────────────────────────────────────────────────
# Regex-based fake data generation
# ─────────────────────────────────────────────────────────────────────────────

def fake_from_regex(rng: KeyedRandom, pattern: str, *, deterministic: bool = True) -> str:
    """Generate a fake string matching *pattern*.

    When ``rstr`` is installed it is always used for full regex support; in
    deterministic mode it is driven by a ``random.Random`` seeded from the
    keyed PRNG, so the same (run key, column, value) maps to the same output
    within a run. Without ``rstr``, a built-in generator handles escapes,
    charsets, groups, alternation, and quantifiers.
    """
    if _HAS_RSTR:
        import random as _random
        if deterministic:
            seed = int.from_bytes(rng.bytes(8), "big")
            return _rstr.Rstr(_random.Random(seed)).xeger(pattern)
        return _rstr.xeger(pattern)
    return _basic_regex_gen(rng, pattern)


def _split_top_level(p: str, sep: str) -> list[str]:
    """Split on *sep* at nesting depth 0 (ignores ``[...]`` and ``(...)``)."""
    parts: list[str] = []
    depth = 0
    in_class = False
    buf: list[str] = []
    i = 0
    while i < len(p):
        ch = p[i]
        if ch == "\\" and i + 1 < len(p):
            buf.append(p[i : i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            buf.append(ch)
        elif ch == "[":
            in_class = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _match_paren(p: str, start: int) -> int:
    """Index of the ``)`` matching the ``(`` at *start* (escape/class aware)."""
    depth = 0
    in_class = False
    i = start
    while i < len(p):
        ch = p[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unbalanced parentheses in pattern: {p!r}")


def _basic_regex_gen(rng: KeyedRandom, pattern: str) -> str:
    """Fallback regex generator for when *rstr* is not installed.

    Handles: ``\\d`` / ``\\w`` / ``\\s`` escapes, ``[charset]`` (with ranges),
    groups ``(...)`` with alternation ``a|b``, and quantifiers ``{n}``,
    ``{n,m}``, ``?``, ``+``, ``*``. Each repetition is generated
    independently. Not a full regex engine — install ``rstr`` for complete
    support (lookarounds, backreferences, etc.).
    """
    p = pattern.strip("^$")

    # Top-level alternation: pick one branch.
    branches = _split_top_level(p, "|")
    if len(branches) > 1:
        p = branches[rng.randint(0, len(branches) - 1)]

    out: list[str] = []
    i = 0
    while i < len(p):
        # Build a generator for the next token, then apply any quantifier.
        if p[i] == "\\" and i + 1 < len(p):
            esc = p[i + 1]
            gen = (lambda c: lambda: _gen_escaped(rng, c))(esc)
            i += 2
        elif p[i] == "[":
            j = p.index("]", i + 1)
            charset = p[i + 1 : j]
            gen = (lambda cs: lambda: _gen_charset(rng, cs))(charset)
            i = j + 1
        elif p[i] == "(":
            j = _match_paren(p, i)
            inner = p[i + 1 : j]
            if inner.startswith("?:"):
                inner = inner[2:]
            gen = (lambda sub: lambda: _basic_regex_gen(rng, sub))(inner)
            i = j + 1
        else:
            lit = p[i]
            gen = (lambda c: lambda: c)(lit)
            i += 1

        if i < len(p) and p[i] == "{":
            j = p.index("}", i + 1)
            parts = p[i + 1 : j].split(",")
            if len(parts) == 1:
                repeat = int(parts[0])
            else:
                lo = int(parts[0]) if parts[0] else 0
                hi = int(parts[1]) if parts[1] else lo + 3
                repeat = rng.randint(lo, hi)
            i = j + 1
        elif i < len(p) and p[i] in "?+*":
            q = p[i]
            i += 1
            if q == "?":
                repeat = rng.randint(0, 1)
            elif q == "+":
                repeat = rng.randint(1, 3)
            else:  # *
                repeat = rng.randint(0, 3)
        else:
            repeat = 1

        out.append("".join(gen() for _ in range(repeat)))

    return "".join(out)


def _gen_escaped(rng: KeyedRandom, ch: str) -> str:
    if ch == "d":
        return str(rng.randint(0, 9))
    if ch == "w":
        return rng.choice("abcdefghijklmnopqrstuvwxyz0123456789")
    if ch == "s":
        return " "
    return ch  # literal (e.g. \. → .)


def _gen_charset(rng: KeyedRandom, charset: str) -> str:
    """Expand a bracket expression like ``0-9a-fA-F`` and pick one."""
    expanded: list[str] = []
    k = 0
    while k < len(charset):
        if k + 2 < len(charset) and charset[k + 1] == "-":
            for c in range(ord(charset[k]), ord(charset[k + 2]) + 1):
                expanded.append(chr(c))
            k += 3
        else:
            expanded.append(charset[k])
            k += 1
    return rng.choice(expanded) if expanded else "?"


# ─────────────────────────────────────────────────────────────────────────────
# Capability report (for the UI/CLI to show what's available)
# ─────────────────────────────────────────────────────────────────────────────

def capabilities() -> dict:
    return {
        "faker": _HAS_FAKER,
        "aes_gcm": _HAS_AESGCM,
        "ff3": _HAS_FF3,
        "rstr": _HAS_RSTR,
        "position_slicing": True,
        "fake_kinds": ["name", "phone", "email", "address", "company",
                       "national_id", "credit_card", "iban", "date", "uuid",
                       "free_text", "regex"],
        "default_locale_options": ["default", "ar"],
        "faker_locale_options": ["default", "en", "ar"],
        "locales": ["default", "ar", "en"],
    }
