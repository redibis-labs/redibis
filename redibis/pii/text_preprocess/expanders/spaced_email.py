"""Obfuscated / spaced email expander."""

from __future__ import annotations

import re
from functools import lru_cache

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.expanders._util import fold_ar, load_yaml_lexicon
from redibis.pii.text_preprocess.registry import register_text_expander
from redibis.pii.text_preprocess.surface import SurfaceSpan


@lru_cache(maxsize=1)
def _token_sets() -> tuple[set[str], set[str]]:
    data = load_yaml_lexicon("at_tokens.yaml")
    at_tokens = {fold_ar(t) for t in (data.get("at_tokens") or [])}
    dot_tokens = {fold_ar(t) for t in (data.get("dot_tokens") or [])}
    at_tokens.update({"@", "＠"})
    dot_tokens.update({".", "．"})
    return at_tokens, dot_tokens


# Local part: contiguous or lightly spaced alnum (not free English words)
_LOCAL = r"[A-Za-z0-9._%+\-](?:\s*[A-Za-z0-9._%+\-]){0,24}"
_DOMAIN_LABEL = r"[A-Za-z0-9\-](?:\s*[A-Za-z0-9\-]){0,24}"
_TLD = r"[A-Za-z](?:\s*[A-Za-z]){1,8}"

_SPACED_AT = re.compile(
    rf"(?<!\w)({_LOCAL})\s*(?:@|［at］|\[at\]|\(at\)|آت)\s*"
    rf"({_DOMAIN_LABEL})\s*(?:\.|［dot］|\[dot\]|\(dot\)|نقطة|نقطه|دوت)\s*"
    rf"({_TLD})(?!\w)",
    re.IGNORECASE,
)


def _collapse_alnum(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


@register_text_expander("spaced_email")
class SpacedEmailExpander:
    name = "spaced_email"

    def expand(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        if not text or "@" not in text and "at" not in text.lower() and "آت" not in text and "ات" not in text:
            # Still try verbal forms
            if not re.search(r"(?i)\b(at|dot|period)\b|نقطة|آت", text or ""):
                if "@" not in (text or ""):
                    return []

        out: list[SurfaceSpan] = []
        for m in _SPACED_AT.finditer(text or ""):
            local = _collapse_alnum(m.group(1))
            domain = _collapse_alnum(m.group(2))
            tld = _collapse_alnum(m.group(3))
            if not local or not domain or not tld:
                continue
            canonical = f"{local}@{domain}.{tld}"
            # Skip already-clean emails (regex path covers them)
            surface = m.group(0)
            if re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", surface.strip()):
                continue
            out.append(SurfaceSpan(
                start=m.start(),
                end=m.end(),
                surface=surface,
                canonical=canonical,
                variant_kind=self.name,
                entity_hint="EMAIL_ADDRESS",
            ))

        # Token-walk for spaced letters: a h m e d at g m a i l dot c o m
        out.extend(self._token_walk(text or ""))
        return out

    def _token_walk(self, text: str) -> list[SurfaceSpan]:
        at_tokens, dot_tokens = _token_sets()
        tokens = list(re.finditer(r"\S+", text))
        fillers = {
            "email", "mail", "me", "my", "the", "is", "reach", "write", "to",
            "please", "الإيميل", "الايميل", "مسجل", "بيه", "في", "التطبيق",
            "يا", "فندم",
        }
        spans: list[SurfaceSpan] = []
        i = 0
        while i < len(tokens):
            tok = fold_ar(tokens[i].group(0).strip("()[]{}"))
            if tok not in at_tokens:
                i += 1
                continue
            # Collect local tokens immediately before "at", allowing verbal dots
            # ("ahmed dot sayed 89 at …").
            local_parts: list[str] = []
            j = i - 1
            while j >= 0:
                raw = tokens[j].group(0).strip("()[]{},.;:")
                folded = fold_ar(raw)
                if folded in at_tokens:
                    break
                if folded in fillers:
                    break
                if folded in dot_tokens or raw == ".":
                    local_parts.insert(0, ".")
                    j -= 1
                    continue
                if re.fullmatch(r"[A-Za-z0-9._%+\-]{1,24}", raw):
                    local_parts.insert(0, raw)
                    j -= 1
                    if len(local_parts) > 32:
                        break
                    continue
                break
            if not local_parts or all(p == "." for p in local_parts):
                i += 1
                continue

            # Domain + optional verbal dots after
            domain_parts: list[str] = []
            k = i + 1
            dots_seen = 0
            while k < len(tokens):
                raw = tokens[k].group(0).strip("()[]{},.;:")
                folded = fold_ar(raw)
                if folded in {"please", "thanks", "today", "now", "asap", "يا", "فندم"}:
                    break
                if folded in dot_tokens or raw == ".":
                    dots_seen += 1
                    domain_parts.append(".")
                    k += 1
                    continue
                if folded in at_tokens:
                    break
                if re.fullmatch(r"[A-Za-z0-9\-]{1,24}", raw):
                    domain_parts.append(raw)
                    k += 1
                    if len(domain_parts) > 30:
                        break
                    continue
                break
            if dots_seen < 1 or len(domain_parts) < 3:
                i += 1
                continue

            start = tokens[j + 1].start()
            end = tokens[k - 1].end()
            # Rebuild local: collapse around verbal dots
            local_rebuilt: list[str] = []
            buf = ""
            for part in local_parts:
                if part == ".":
                    if buf:
                        local_rebuilt.append(buf)
                        buf = ""
                    local_rebuilt.append(".")
                else:
                    buf += part
            if buf:
                local_rebuilt.append(buf)
            local = "".join(local_rebuilt).strip(".")
            if not local or local.startswith(".") or ".." in local:
                i += 1
                continue

            rebuilt: list[str] = []
            buf = ""
            for part in domain_parts:
                if part == ".":
                    if buf:
                        rebuilt.append(buf)
                        buf = ""
                    rebuilt.append(".")
                else:
                    buf += part
            if buf:
                rebuilt.append(buf)
            domain = "".join(rebuilt)
            if domain.count(".") < 1 or domain.startswith(".") or domain.endswith("."):
                i += 1
                continue
            canonical = f"{local}@{domain}"
            surface = text[start:end]
            if not re.search(r"(?i)\bat\b|\[at\]|\(at\)|آت", surface) and "@" not in surface:
                i += 1
                continue
            spans.append(SurfaceSpan(
                start=start,
                end=end,
                surface=surface,
                canonical=canonical,
                variant_kind=self.name,
                entity_hint="EMAIL_ADDRESS",
            ))
            i = k
        return spans
