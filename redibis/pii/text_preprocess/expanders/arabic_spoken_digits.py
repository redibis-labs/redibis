"""Egyptian Arabic spoken-digit expander for free-text PII.

Handles call-center / Whisper-style readings including:
- digit words and teens (حداشر → 11)
- round tens (تلاتين → 30)
- compounds (سبعة وستين → 67)
- hundreds (ميتين → 200) and hundreds+addend (ربعمية وخمسين → 450)
- digit plurals (أربعة خمسات → 5555)
- thousands (تلات ألاف وخمسة → 3005)
- Arabic comma separators inside a single run
"""

from __future__ import annotations

import re
from typing import Optional

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.expanders._util import (
    fold_ar,
    fold_indic_digits,
    label_near,
    load_yaml_lexicon,
    noise_set,
    tokenize_with_spans,
)
from redibis.pii.text_preprocess.registry import register_text_expander
from redibis.pii.text_preprocess.surface import SurfaceSpan


def _build_lexicon() -> tuple:
    data = load_yaml_lexicon("spoken_digits_ar.yaml")
    digit_map: dict[str, str] = {}
    teen_map: dict[str, str] = {}
    tens_map: dict[str, str] = {}
    hundreds_map: dict[str, str] = {}
    plurals_map: dict[str, str] = {}
    for digit, words in (data.get("digits") or {}).items():
        for w in words or []:
            digit_map[fold_ar(str(w))] = str(digit)
    for teen, words in (data.get("teens") or {}).items():
        for w in words or []:
            teen_map[fold_ar(str(w))] = str(teen)
    for ten, words in (data.get("tens") or {}).items():
        for w in words or []:
            tens_map[fold_ar(str(w))] = str(ten)
    for hun, words in (data.get("hundreds") or {}).items():
        for w in words or []:
            hundreds_map[fold_ar(str(w))] = str(hun)
    for dig, words in (data.get("digit_plurals") or {}).items():
        for w in words or []:
            plurals_map[fold_ar(str(w))] = str(dig)
    connectors = {fold_ar(c) for c in (data.get("connectors") or [])}
    thousands = {fold_ar(c) for c in (data.get("thousands") or [])}
    phone_labels = list(data.get("phone_labels") or [])
    nid_labels = list(data.get("nid_labels") or [])
    card_labels = list(data.get("card_labels") or [])
    cvv_labels = list(data.get("cvv_labels") or [])
    otp_labels = list(data.get("otp_labels") or [])
    expiry_labels = list(data.get("expiry_labels") or [])
    partial_labels = list(data.get("partial_card_labels") or [])
    voucher_labels = list(data.get("voucher_labels") or [])
    puk_labels = list(data.get("puk_labels") or [])
    tax_labels = list(data.get("tax_labels") or [])
    ip_labels = list(data.get("ip_labels") or [])
    passport_labels = list(data.get("passport_labels") or [])
    return (
        digit_map, teen_map, tens_map, hundreds_map, plurals_map,
        connectors, thousands,
        phone_labels, nid_labels, card_labels, cvv_labels,
        otp_labels, expiry_labels, partial_labels,
        voucher_labels, puk_labels, tax_labels, ip_labels, passport_labels,
    )


(
    _DIGIT_MAP,
    _TEEN_MAP,
    _TENS_MAP,
    _HUNDREDS_MAP,
    _PLURALS_MAP,
    _CONNECTORS,
    _THOUSANDS,
    _PHONE_LABELS,
    _NID_LABELS,
    _CARD_LABELS,
    _CVV_LABELS,
    _OTP_LABELS,
    _EXPIRY_LABELS,
    _PARTIAL_LABELS,
    _VOUCHER_LABELS,
    _PUK_LABELS,
    _TAX_LABELS,
    _IP_LABELS,
    _PASSPORT_LABELS,
) = _build_lexicon()

# Spoken separators / confirmation tokens inside a single run.
_SKIP_SEPS = {
    "-", "–", "—", "/",
    fold_ar("كابيتال"), "capital", fold_ar("حرف"),
}
_DASH_WORDS = {fold_ar("داش"), "dash"}
_DOT_SEPS = {fold_ar("دوت"), "dot"}
_LETTER_MAP = {
    fold_ar("إيه"): "A",
    fold_ar("ايه"): "A",
    fold_ar("اي"): "A",
    "a": "A",
    fold_ar("بي"): "B",
    fold_ar("باء"): "B",
    "b": "B",
    fold_ar("سي"): "C",
    "c": "C",
}

_STRIP_EDGE = re.compile(r"^[\(\[\{«\"'،,.;:!?؟]+|[\)\]\}»\"'،,.;:!?؟]+$")
_SPAN_LEAD = " \t\r\n:：=-–—"
_SPAN_TRAIL = re.compile(r"[\s.،,;:!?؟…»«\"')\]]+$")


def _looks_ipv4(canonical: str) -> bool:
    parts = (canonical or "").split(".")
    if len(parts) != 4:
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _passport_near(text: str, start: int, end: int) -> bool:
    return bool(label_near(text, start, end, _PASSPORT_LABELS, radius=140))


def _trim_spoken_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Drop leading/trailing punctuation glued onto the last spoken token."""
    if start is None or end is None or end <= start or not text:
        return start, end
    surface = text[start:end]
    lead = len(surface) - len(surface.lstrip(_SPAN_LEAD))
    core = _SPAN_TRAIL.sub("", surface[lead:])
    if not core:
        return start, end
    new_start = start + lead
    new_end = new_start + len(core)
    if new_end <= new_start:
        return start, end
    return new_start, new_end


def _clean_token(tok: str) -> str:
    t = fold_indic_digits(tok)
    t = _STRIP_EDGE.sub("", t)
    return fold_ar(t)


def _all_number_words() -> set[str]:
    return (
        set(_DIGIT_MAP) | set(_TEEN_MAP) | set(_TENS_MAP)
        | set(_HUNDREDS_MAP) | set(_PLURALS_MAP) | set(_THOUSANDS)
    )


def _expand_wa_tokens(
    tokens: list[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    """Split attached connector forms like ``وخمسين`` → ``و`` + ``خمسين``."""
    known = _all_number_words()
    out: list[tuple[str, int, int]] = []
    for tok, start, end in tokens:
        cleaned = _clean_token(tok)
        if cleaned.startswith("و") and len(cleaned) > 1:
            rest = cleaned[1:]
            if rest in known or rest in _TENS_MAP or rest in _DIGIT_MAP:
                out.append((tok[0], start, start + 1))
                out.append((tok[1:], start + 1, end))
                continue
        out.append((tok, start, end))
    return out


def _parse_addend(
    tokens: list[tuple[str, int, int]],
    idx: int,
) -> Optional[tuple[int, int]]:
    """Parse a small addend (unit, tens, or unit+و+tens). Returns (value, n_tok)."""
    if idx >= len(tokens):
        return None
    cleaned = _clean_token(tokens[idx][0])
    if cleaned in _DIGIT_MAP and idx + 2 < len(tokens):
        mid = _clean_token(tokens[idx + 1][0])
        right = _clean_token(tokens[idx + 2][0])
        if mid in _CONNECTORS and right in _TENS_MAP:
            return int(_DIGIT_MAP[cleaned]) + int(_TENS_MAP[right]), 3
        if mid in _CONNECTORS and right in _TEEN_MAP:
            return int(_DIGIT_MAP[cleaned]) + int(_TEEN_MAP[right]), 3
    if cleaned in _TENS_MAP:
        return int(_TENS_MAP[cleaned]), 1
    if cleaned in _TEEN_MAP:
        return int(_TEEN_MAP[cleaned]), 1
    if cleaned in _DIGIT_MAP:
        return int(_DIGIT_MAP[cleaned]), 1
    return None


def _parse_atom(
    tokens: list[tuple[str, int, int]],
    idx: int,
    *,
    prefer_teens: bool,
) -> Optional[tuple[str, int]]:
    """Parse one number atom starting at ``idx``.

    Returns ``(digit_chars, tokens_consumed)`` or ``None``.
    """
    if idx >= len(tokens):
        return None
    cleaned = _clean_token(tokens[idx][0])
    if not cleaned:
        return None

    if cleaned.isdigit():
        return (cleaned, 1) if len(cleaned) == 1 else None

    # Thousands: تلات ألاف [و خمسة] → 3000 or 3005
    if cleaned in _DIGIT_MAP and idx + 1 < len(tokens):
        nxt = _clean_token(tokens[idx + 1][0])
        if nxt in _THOUSANDS:
            base = int(_DIGIT_MAP[cleaned]) * 1000
            n_tok = 2
            if idx + 2 < len(tokens) and _clean_token(tokens[idx + 2][0]) in _CONNECTORS:
                add = _parse_addend(tokens, idx + 3)
                if add is not None:
                    base += add[0]
                    n_tok = 3 + add[1]
            return (str(base), n_tok)

    # Digit plural: أربعة خمسات → 5555
    if cleaned in _DIGIT_MAP and idx + 1 < len(tokens):
        plural = _clean_token(tokens[idx + 1][0])
        if plural in _PLURALS_MAP:
            count = int(_DIGIT_MAP[cleaned])
            dig = _PLURALS_MAP[plural]
            if 1 <= count <= 8:
                return (dig * count, 2)

    # Compound: unit + و + tens → سبعة وستين = 67
    if cleaned in _DIGIT_MAP and idx + 2 < len(tokens):
        mid = _clean_token(tokens[idx + 1][0])
        right = _clean_token(tokens[idx + 2][0])
        if mid in _CONNECTORS and right in _TENS_MAP:
            unit = int(_DIGIT_MAP[cleaned])
            ten = int(_TENS_MAP[right])
            return (f"{ten + unit:02d}", 3)

    # Hundreds + و + addend → ربعمية وخمسين = 450, ميتين وتمنين = 280
    if cleaned in _HUNDREDS_MAP:
        hun = int(_HUNDREDS_MAP[cleaned])
        if idx + 2 < len(tokens) and _clean_token(tokens[idx + 1][0]) in _CONNECTORS:
            add = _parse_addend(tokens, idx + 2)
            if add is not None:
                return (f"{hun + add[0]:03d}", 2 + add[1])
        return (f"{hun:03d}", 1)

    if prefer_teens and cleaned in _TEEN_MAP:
        return (_TEEN_MAP[cleaned], 1)

    if cleaned in _TENS_MAP:
        return (_TENS_MAP[cleaned], 1)

    if cleaned in _DIGIT_MAP:
        return (_DIGIT_MAP[cleaned], 1)

    if cleaned in _TEEN_MAP:
        return (_TEEN_MAP[cleaned], 1)

    return None


def _nearest_labeled(
    text: str,
    start: int,
    end: int,
    groups: list[tuple[str, list[str], int]],
) -> tuple[str, str]:
    """Pick the closest label among candidate groups.

    ``groups`` entries are ``(hint, labels, radius)``. Returns ``(hint, label)``
    or ``("", "")`` when nothing matches.
    """
    best_hint = ""
    best_label = ""
    best_dist = 10**9
    for hint, labels, radius in groups:
        for lab in labels:
            folded_lab = fold_ar(lab)
            if not folded_lab:
                continue
            # Search in a window around the span
            left = max(0, start - radius)
            right = min(len(text), end + radius)
            window = text[left:right]
            folded_win = fold_ar(window)
            pos = folded_win.find(folded_lab)
            if pos < 0:
                continue
            # Approximate distance from span start in original coords
            abs_pos = left + pos
            if abs_pos + len(lab) <= start:
                dist = start - (abs_pos + len(lab))
            elif abs_pos >= end:
                dist = abs_pos - end
            else:
                dist = 0
            if dist < best_dist:
                best_dist = dist
                best_hint = hint
                best_label = lab
    return best_hint, best_label


def _classify_run(
    text: str,
    start: int,
    end: int,
    canonical: str,
    *,
    prefer_teens: bool,
) -> tuple[str, str, bool]:
    """Return (entity_hint, label, context_boost)."""
    digits_only = re.sub(r"\D", "", canonical)
    n = len(digits_only)
    letters = re.sub(r"[^A-Za-z]", "", canonical)
    if n == 9:
        tax_label = label_near(text, start, end, _TAX_LABELS, radius=140)
        if tax_label:
            return "EG_TAX_ID", tax_label, True
    if _looks_ipv4(canonical) or canonical.count(".") == 3:
        ip_label = label_near(text, start, end, _IP_LABELS, radius=120)
        return "IP_ADDRESS", ip_label, True
    if letters and n == 8 and re.fullmatch(r"[A-Za-z]\d{8}", canonical):
        pass_label = label_near(text, start, end, _PASSPORT_LABELS, radius=140)
        if pass_label:
            return "PASSPORT", pass_label, True
    if canonical.count("-") == 2 and n == 9:
        tax_label = label_near(text, start, end, _TAX_LABELS, radius=140)
        if tax_label:
            return "EG_TAX_ID", tax_label, True
    hint, label = _nearest_labeled(
        text,
        start,
        end,
        [
            ("CVV", _CVV_LABELS, 60),
            ("PASSPORT", _PASSPORT_LABELS, 120),
            ("EG_TAX_ID", _TAX_LABELS, 120),
            ("IP_ADDRESS", _IP_LABELS, 100),
            ("OTP", _OTP_LABELS, 80),
            ("SIM_PUK", _PUK_LABELS, 100),
            ("VOUCHER", _VOUCHER_LABELS, 160),
            ("CREDIT_CARD_EXPIRATION", _EXPIRY_LABELS, 80),
            ("CREDIT_CARD", _PARTIAL_LABELS, 80),
            ("CREDIT_CARD", _CARD_LABELS, 100),
            ("EG_NATIONAL_ID", _NID_LABELS, 120),
            ("PHONE_NUMBER", _PHONE_LABELS, 80),
        ],
    )
    # Structural overrides when length strongly indicates a type.
    if hint == "CVV" and not (3 <= n <= 4):
        hint, label = "", ""
    if hint == "OTP" and n == 8:
        puk_label = label_near(text, start, end, _PUK_LABELS, radius=100)
        if puk_label:
            hint, label = "SIM_PUK", puk_label
    if hint == "OTP" and not (4 <= n <= 8):
        hint, label = "", ""
    if hint == "CREDIT_CARD_EXPIRATION" and n not in (3, 4):
        hint, label = "", ""
    if hint == "SIM_PUK" and n != 8:
        hint, label = "", ""
    if hint == "VOUCHER" and not (10 <= n <= 16):
        hint, label = "", ""
    if hint == "EG_TAX_ID" and n != 9:
        hint, label = "", ""
    if hint == "PASSPORT" and not (n == 8 and letters):
        hint, label = "", ""
    if hint == "IP_ADDRESS" and not _looks_ipv4(canonical):
        hint, label = "", ""
    if n >= 15:
        card_label = label_near(text, start, end, _CARD_LABELS, radius=140)
        if card_label and hint in {"", "PHONE_NUMBER", "CREDIT_CARD"}:
            return "CREDIT_CARD", card_label, True
    if n == 14:
        voucher_label = label_near(text, start, end, _VOUCHER_LABELS, radius=200)
        if voucher_label and hint in {"", "CREDIT_CARD", "PHONE_NUMBER"}:
            return "VOUCHER", voucher_label, True
    if hint == "CREDIT_CARD" and n >= 12:
        # 14-digit Egyptian NID near "قومي" wins over ambient "كارت".
        nid_label = label_near(text, start, end, _NID_LABELS, radius=120)
        if nid_label and n == 14:
            return "EG_NATIONAL_ID", nid_label, True
    if not hint:
        voucher_label = label_near(text, start, end, _VOUCHER_LABELS, radius=160)
        if voucher_label and 10 <= n <= 16:
            return "VOUCHER", voucher_label, True
        puk_label = label_near(text, start, end, _PUK_LABELS, radius=100)
        if puk_label and n == 8:
            return "SIM_PUK", puk_label, True
        if n == 14:
            nid_label = label_near(text, start, end, _NID_LABELS, radius=120)
            return "EG_NATIONAL_ID", nid_label, bool(nid_label)
        if 15 <= n <= 19:
            card_label = label_near(text, start, end, _CARD_LABELS, radius=100)
            return "CREDIT_CARD", card_label, bool(card_label)
        if phone_label := label_near(text, start, end, _PHONE_LABELS, radius=80):
            return "PHONE_NUMBER", phone_label, True
        if prefer_teens and 8 <= n <= 13:
            return "PHONE_NUMBER", "", False
        if 8 <= n <= 13:
            return "PHONE_NUMBER", "", False
    return hint, label, bool(label)


@register_text_expander("arabic_spoken_digits")
class ArabicSpokenDigitsExpander:
    """Find runs of Egyptian Arabic spoken digits (call-center style)."""

    name = "arabic_spoken_digits"
    min_digits_long = 8
    min_digits_short = 3
    max_digits = 20

    def expand(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        if not text:
            return []
        arabic = bool(getattr(ctx, "arabic", False)) or (ctx.language or "").startswith("ar")
        if not arabic and not re.search(r"[\u0600-\u06FF]", text):
            return []

        tokens = _expand_wa_tokens(tokenize_with_spans(text))
        spans: list[SurfaceSpan] = []
        seen: set[tuple[int, int, str]] = set()
        noise = noise_set(ctx)
        i = 0
        while i < len(tokens):
            emitted = False
            for prefer_teens in (True, False):
                run = self._consume_run(
                    tokens, i, text, prefer_teens=prefer_teens, noise=noise,
                )
                if run is None:
                    continue
                start, end, canonical, consumed = run
                start, end = _trim_spoken_span(text, start, end)
                n = len(re.sub(r"[^A-Za-z0-9]", "", canonical))
                if n < self.min_digits_short or n > self.max_digits:
                    continue
                hint, label, boost = _classify_run(
                    text, start, end, canonical, prefer_teens=prefer_teens
                )
                # Long runs always eligible; short runs need a contextual label/hint.
                if n < self.min_digits_long and not hint:
                    continue
                if not hint:
                    if n == 14:
                        hint = "EG_NATIONAL_ID"
                    elif n == 8 and label_near(text, start, end, _PUK_LABELS, radius=100):
                        hint = "SIM_PUK"
                    elif 8 <= n <= 13:
                        hint = "PHONE_NUMBER"
                    elif 15 <= n <= 16:
                        hint = "CREDIT_CARD"
                key = (start, end, canonical)
                if key in seen:
                    i += max(consumed, 1)
                    emitted = True
                    break
                seen.add(key)
                # Pretty-print expiry as MM/YY when we have 4 digits.
                canon_out = canonical
                if hint == "CREDIT_CARD_EXPIRATION" and len(canonical) == 4:
                    canon_out = f"{canonical[:2]}/{canonical[2:]}"
                elif hint == "CREDIT_CARD_EXPIRATION" and len(canonical) == 3:
                    canon_out = f"0{canonical[0]}/{canonical[1:]}"
                spans.append(SurfaceSpan(
                    start=start,
                    end=end,
                    surface=text[start:end],
                    canonical=canon_out,
                    variant_kind=self.name,
                    entity_hint=hint,
                    context_boost=boost,
                    label=label,
                ))
                i += max(consumed, 1)
                emitted = True
                break
            if not emitted:
                i += 1
        return spans

    def _consume_run(
        self,
        tokens: list[tuple[str, int, int]],
        start_idx: int,
        text: str,
        *,
        prefer_teens: bool,
        noise: frozenset[str] = frozenset(),
    ) -> Optional[tuple[int, int, str, int]]:
        first_tok = tokens[start_idx][0]
        first_clean = _clean_token(first_tok)
        if first_clean.isdigit():
            return None

        digits: list[str] = []
        spoken_atoms = 0
        first_start: Optional[int] = None
        last_end: Optional[int] = None
        consumed = 0
        idx = start_idx

        while idx < len(tokens):
            tok_start, tok_end = tokens[idx][1], tokens[idx][2]
            cleaned = _clean_token(tokens[idx][0])
            # Noise tokens are transparent: skip without extending last_end or
            # counting as spoken atoms, same as _SKIP_SEPS. A filler *between*
            # digits still sits inside first_start..last_end (contiguous span);
            # leading/trailing fillers stay outside because last_end is not
            # advanced. Middle fillers cannot be excluded without breaking
            # text[start:end] integrity.
            if not cleaned or cleaned in _SKIP_SEPS or cleaned in noise:
                idx += 1
                consumed += 1
                continue
            if cleaned in _DASH_WORDS:
                if digits:
                    digits.append("-")
                    last_end = tok_end
                    idx += 1
                    consumed += 1
                    continue
                break
            if cleaned in _DOT_SEPS:
                if digits:
                    digits.append(".")
                    last_end = tok_end
                    idx += 1
                    consumed += 1
                    continue
                break
            if cleaned in _LETTER_MAP:
                probe_start = tok_start if first_start is None else first_start
                if _passport_near(text, probe_start, tok_end):
                    if first_start is None:
                        first_start = tok_start
                    last_end = tok_end
                    digits.append(_LETTER_MAP[cleaned])
                    spoken_atoms += 1
                    idx += 1
                    consumed += 1
                    continue
            if cleaned.isdigit() and len(cleaned) > 1:
                # Parenthetical confirmation of a spoken value, e.g. (15).
                idx += 1
                consumed += 1
                continue
            if cleaned in _CONNECTORS:
                if digits and idx + 1 < len(tokens):
                    nxt = _parse_atom(tokens, idx + 1, prefer_teens=prefer_teens)
                    if nxt is not None:
                        idx += 1
                        consumed += 1
                        continue
                break

            atom = _parse_atom(tokens, idx, prefer_teens=prefer_teens)
            if atom is None:
                break
            chunk, n_tok = atom
            spoken_atoms += 1
            if first_start is None:
                first_start = tokens[idx][1]
            last_end = tokens[idx + n_tok - 1][2]
            digits.append(chunk)
            consumed += n_tok
            idx += n_tok

            payload = re.sub(r"[^A-Za-z0-9]", "", "".join(digits))
            if len(payload) >= self.max_digits:
                break

        if first_start is None or last_end is None:
            return None
        canonical = "".join(digits)
        # One compound atom is enough (سبعمية وتمنية → 708, تلات ألاف → 3000).
        if spoken_atoms < 1:
            return None
        payload = re.sub(r"[^A-Za-z0-9]", "", canonical)
        if len(payload) < self.min_digits_short:
            return None
        return first_start, last_end, canonical, consumed
