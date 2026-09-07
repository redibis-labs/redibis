"""Per-entity column-name context tokens (locale-pack inputs).

Extracted from ``regex_catalog.CATALOG`` so the built-in set preserves today's
bilingual EN+AR hints. Pack ``locale/tokens.yaml`` overlays union / remove.
Never imports Presidio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from redibis.pii.token_normalize import DEFAULT_NORMALIZERS, apply_normalizers

_COL_SPLIT = re.compile(r"[^a-z0-9؀-ۿ]+", re.IGNORECASE)


#: Column-name tokens that **suppress** an entity before any value is parsed.
#: Matching is on token boundaries after splitting on ``[^a-z0-9]+``.
NEGATIVE_TOKENS: dict[str, tuple[str, ...]] = {
    "LOCATION": (
        "age", "score", "rating", "pct", "percent", "ratio", "rate",
        "count", "qty", "quantity", "temp", "temperature", "amount",
        "price", "discount", "weight", "height", "duration", "latency",
        "day", "week", "month", "year", "version", "level", "priority",
    ),
    "IMEI": (
        "ts", "time", "timestamp", "epoch", "seq", "sequence", "serial",
        "amount", "balance", "bytes", "volume",
    ),
    "IMSI": (
        "ts", "time", "timestamp", "epoch", "seq", "sequence",
    ),
    "EG_NATIONAL_ID": (
        "ts", "time", "timestamp", "epoch", "datetime", "dt",
        "created", "updated", "modified", "inserted", "loaded",
    ),
}


@dataclass(frozen=True)
class ContextTokenSet:
    """Column-name tokens that boost confidence for one entity."""

    entity_type: str
    tokens: tuple[str, ...]
    normalize: tuple[str, ...] = DEFAULT_NORMALIZERS


def _tokens_from_catalog() -> dict[str, tuple[str, ...]]:
    from redibis.pii.regex_catalog import CATALOG

    by_entity: dict[str, set[str]] = {}
    for entry in CATALOG.values():
        ent = entry.entity_type
        if not ent:
            continue
        bucket = by_entity.setdefault(ent, set())
        for hint in entry.context_hints or ():
            h = str(hint).strip()
            if h:
                bucket.add(h)
    return {k: tuple(sorted(v, key=lambda x: (x.isascii(), x))) for k, v in by_entity.items()}


# Lazily populated — catalog import is heavy but already required by PII.
_BUILTIN: dict[str, tuple[str, ...]] | None = None


def builtin_context_tokens() -> dict[str, tuple[str, ...]]:
    """Today's EN+AR tokens grouped by entity (parity with inline catalog hints)."""
    global _BUILTIN
    if _BUILTIN is None:
        _BUILTIN = _tokens_from_catalog()
    return dict(_BUILTIN)


def parse_tokens_document(raw: Any) -> tuple[dict[str, tuple[str, ...]], list[str], tuple[str, ...]]:
    """Parse ``locale/tokens.yaml``.

    Accepted shapes::

        PHONE_NUMBER: [phone, هاتف]
        PERSON:
          tokens: [nom, prénom]
          normalize: [casefold, accent_fold]
        remove: [EG_VEHICLE_PLATE]   # drop entire entity token set
        normalize: [casefold, accent_fold]  # default for simple lists
    """
    if raw is None:
        return {}, [], DEFAULT_NORMALIZERS
    if not isinstance(raw, dict):
        raise ValueError("locale/tokens.yaml must be a mapping")

    remove = [str(x) for x in (raw.get("remove") or [])]
    default_norm = tuple(raw.get("normalize") or DEFAULT_NORMALIZERS)
    tokens: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if key in {"remove", "normalize"}:
            continue
        if isinstance(value, dict):
            toks = value.get("tokens") or value.get("add") or []
            tokens[str(key)] = tuple(str(t) for t in toks)
        elif isinstance(value, (list, tuple)):
            tokens[str(key)] = tuple(str(t) for t in value)
        else:
            raise ValueError(f"invalid tokens entry for {key!r}")
    return tokens, remove, default_norm


def merge_context_tokens(
    base: Mapping[str, Sequence[str]] | None,
    overlay: Mapping[str, Sequence[str]] | None,
    *,
    remove: Sequence[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Union per entity; ``remove`` drops whole entity keys from the result."""
    out: dict[str, set[str]] = {}
    for src in (base or {}, overlay or {}):
        for entity, toks in src.items():
            bucket = out.setdefault(str(entity), set())
            for t in toks:
                if t:
                    bucket.add(str(t))
    for entity in remove or ():
        out.pop(str(entity), None)
    return {k: tuple(sorted(v, key=lambda x: (x.isascii(), x))) for k, v in out.items()}


def merge_negative_tokens(
    base: Mapping[str, Sequence[str]] | None = None,
    overlay: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Union built-in ``NEGATIVE_TOKENS`` with operator overlays per entity."""
    return merge_context_tokens(base or NEGATIVE_TOKENS, overlay)


def column_has_negative_token(
    column_name: str,
    entity_type: str | None,
    *,
    negative_tokens: Mapping[str, Sequence[str]] | None = None,
    normalizers: Sequence[str] | None = None,
) -> bool:
    """True when a negative token for *entity_type* matches the column name.

    Matching is on token boundaries only (no substring) so ``latency`` does
    not match ``lat`` and ``long_text`` does not match ``long``.
    """
    if not entity_type or not column_name:
        return False
    table = negative_tokens if negative_tokens is not None else NEGATIVE_TOKENS
    hints = table.get(entity_type) or table.get(entity_type.upper()) or ()
    if not hints:
        return False
    norms = tuple(normalizers or DEFAULT_NORMALIZERS)
    blob = apply_normalizers(column_name, norms)
    tokens = {t for t in _COL_SPLIT.split(blob) if t}
    for hint in hints:
        h = apply_normalizers(str(hint), norms)
        if h and h in tokens:
            return True
    return False


def suppress_entities_for_column(
    column_name: str,
    *,
    negative_tokens: Mapping[str, Sequence[str]] | None = None,
) -> frozenset[str]:
    """Entity types that must not be scanned for this column name."""
    table = negative_tokens if negative_tokens is not None else NEGATIVE_TOKENS
    blocked: set[str] = set()
    for entity in table:
        if column_has_negative_token(
            column_name, entity, negative_tokens=table,
        ):
            blocked.add(entity)
    return frozenset(blocked)


def column_matches_hints(
    column_name: str,
    hints: Sequence[str],
    *,
    normalizers: Sequence[str] | None = None,
) -> bool:
    """True when any hint matches a column token or substring after normalization."""
    if not hints or not column_name:
        return False
    norms = tuple(normalizers or DEFAULT_NORMALIZERS)
    blob = apply_normalizers(column_name, norms)
    tokens = {t for t in _COL_SPLIT.split(blob) if t}
    for hint in hints:
        h = apply_normalizers(str(hint), norms)
        if not h:
            continue
        if h in tokens or h in blob:
            return True
    return False


def effective_hints_for_pattern(
    pattern_hints: Sequence[str],
    entity_type: Optional[str],
    entity_tokens: Mapping[str, Sequence[str]] | None,
) -> tuple[str, ...]:
    """Union pattern-specific hints with locale entity tokens."""
    merged: list[str] = list(pattern_hints or ())
    if entity_type and entity_tokens:
        merged.extend(entity_tokens.get(entity_type) or ())
    # Preserve order, drop dupes
    seen: set[str] = set()
    out: list[str] = []
    for h in merged:
        key = str(h)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)
