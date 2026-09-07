"""
Quality validator registry — built-in engines + optional entry-point plugins.
"""

from __future__ import annotations

import sys
from typing import Callable, Type, TYPE_CHECKING

from redibis.config import ConfigError

if TYPE_CHECKING:
    from redibis.quality.validator import Validator

_BUILTIN: dict[str, type] = {}
_ENTRY_POINT_GROUP = "redibis.validators"


def register_validator(name: str) -> Callable[[type], type]:
    """Decorator: ``@register_validator("great_expectations")``."""

    def _decorator(cls: type) -> type:
        key = name.lower()
        _BUILTIN[key] = cls
        return cls

    return _decorator


def _load_entry_point_validators() -> dict[str, type]:
    loaded: dict[str, type] = {}
    if sys.version_info >= (3, 10):
        from importlib.metadata import entry_points

        eps = entry_points()
        group = (
            eps.select(group=_ENTRY_POINT_GROUP)
            if hasattr(eps, "select")
            else eps.get(_ENTRY_POINT_GROUP, [])
        )
    else:
        from importlib.metadata import entry_points as ep_legacy

        group = ep_legacy().get(_ENTRY_POINT_GROUP, [])

    for ep in group:
        cls = ep.load()
        from redibis.quality.validator import Validator

        if not isinstance(cls, type) or not issubclass(cls, Validator):
            raise ConfigError(
                f"validator entry point {ep.name!r} must resolve to a Validator subclass"
            )
        loaded[ep.name.lower()] = cls
    return loaded


def validator_registry() -> dict[str, type]:
    merged = _load_entry_point_validators()
    merged.update(_BUILTIN)
    return dict(merged)


def get_validator_class(engine: str) -> type:
    if not _BUILTIN:
        from redibis.quality import validator as _validator_mod  # noqa: F401
    key = (engine or "great_expectations").lower()
    cls = validator_registry().get(key)
    if cls is None:
        raise ConfigError(
            f"unknown quality validator {engine!r}; "
            f"choices: {sorted(validator_registry())}"
        )
    return cls
