"""Protocols and registry for free-text obfuscation expanders / validators."""

from __future__ import annotations

import sys
from typing import Callable, Protocol, Sequence, runtime_checkable

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.surface import SurfaceSpan, ValidationOutcome

_EXPANDERS: dict[str, type] = {}
_VALIDATORS: dict[str, object] = {}
_ENTRY_POINT_GROUP = "redibis.text_expanders"


@runtime_checkable
class TextExpander(Protocol):
    """Find obfuscated surface expressions on original Unicode text."""

    name: str

    def expand(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]: ...


@runtime_checkable
class TextValidator(Protocol):
    """Validate a canonicalized value and assign an entity type."""

    name: str
    entity_types: frozenset[str]

    def validate(
        self,
        canonical: str,
        *,
        ctx: RecognizeContext,
        entity_hint: str = "",
        label: str = "",
    ) -> ValidationOutcome: ...


def register_text_expander(name: str) -> Callable[[type], type]:
    """Decorator: ``@register_text_expander("arabic_spoken_digits")``."""

    def _decorator(cls: type) -> type:
        _EXPANDERS[name.lower()] = cls
        return cls

    return _decorator


def register_text_validator(name: str) -> Callable[[object], object]:
    """Decorator / registrar for validator instances or classes."""

    def _decorator(obj: object) -> object:
        _VALIDATORS[name.lower()] = obj
        return obj

    return _decorator


def _load_entry_point_expanders() -> dict[str, type]:
    loaded: dict[str, type] = {}
    try:
        if sys.version_info >= (3, 10):
            from importlib.metadata import entry_points

            eps = entry_points()
            group = (
                eps.select(group=_ENTRY_POINT_GROUP)
                if hasattr(eps, "select")
                else eps.get(_ENTRY_POINT_GROUP, [])  # type: ignore[arg-type]
            )
        else:
            from importlib.metadata import entry_points as ep_legacy

            group = ep_legacy().get(_ENTRY_POINT_GROUP, [])
    except Exception:
        return loaded

    for ep in group or []:
        try:
            cls = ep.load()
        except Exception:
            continue
        if isinstance(cls, type):
            loaded[ep.name.lower()] = cls
    return loaded


def _ensure_builtins_loaded() -> None:
    if not _EXPANDERS:
        # Side-effect registration of built-in expanders.
        from redibis.pii.text_preprocess.expanders import (  # noqa: F401
            age_phrase,
            arabic_spoken_digits,
            digit_cluster,
            parenthesized_digits,
            spaced_email,
            labeled_secret,
        )
    if not _VALIDATORS:
        from redibis.pii.text_preprocess import validators as _validators  # noqa: F401


def expander_registry() -> dict[str, type]:
    _ensure_builtins_loaded()
    merged = _load_entry_point_expanders()
    merged.update(_EXPANDERS)
    return dict(merged)


def validator_registry() -> dict[str, object]:
    _ensure_builtins_loaded()
    return dict(_VALIDATORS)


def get_expander_class(name: str) -> type:
    key = (name or "").lower()
    cls = expander_registry().get(key)
    if cls is None:
        raise KeyError(
            f"unknown text expander {name!r}; choices: {sorted(expander_registry())}"
        )
    return cls


def resolve_expanders(names: Sequence[str] | None = None) -> list[TextExpander]:
    """Instantiate registered expanders. Empty/None → all builtins."""
    registry = expander_registry()
    keys = [n.lower() for n in names] if names else sorted(registry)
    out: list[TextExpander] = []
    for key in keys:
        cls = registry.get(key)
        if cls is None:
            continue
        out.append(cls())  # type: ignore[call-arg]
    return out


def resolve_validators(names: Sequence[str] | None = None) -> list[TextValidator]:
    registry = validator_registry()
    keys = [n.lower() for n in names] if names else sorted(registry)
    out: list[TextValidator] = []
    for key in keys:
        obj = registry.get(key)
        if obj is None:
            continue
        if isinstance(obj, type):
            out.append(obj())  # type: ignore[call-arg]
        else:
            out.append(obj)  # type: ignore[arg-type]
    return out


__all__ = [
    "TextExpander",
    "TextValidator",
    "register_text_expander",
    "register_text_validator",
    "expander_registry",
    "validator_registry",
    "get_expander_class",
    "resolve_expanders",
    "resolve_validators",
]
