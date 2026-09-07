"""
redibis.pii.regex_overrides
============================
Per-run regex catalog overrides.

Two modes
---------
add_regex       Add custom patterns on top of the default CATALOG.
                The default patterns remain active; your additions are merged in.

replace_all     Ignore the default CATALOG entirely.
                Only the patterns you provide in ``add`` are used.

Usage (add mode — default)
--------------------------
    from redibis.pii.regex_overrides import RegexOverrides

    overrides = RegexOverrides(
        add={
            "my_custom_phone": {
                "pattern":          r"^\\+1\\d{10}$",
                "entity_type":      "PHONE_NUMBER",
                "recognizer_group": "structured",
                "presidio_score":   0.90,
                "context_hints":    ("phone", "mobile"),
            },
        },
    )
    # Pass to detect_pii():
    from redibis.pii.detector import detect_pii
    detections = detect_pii(df, regex_overrides=overrides)

Usage (replace_all mode)
------------------------
    overrides = RegexOverrides(
        replace_all=True,
        add={
            "only_this_pattern": {
                "pattern":          r"^EG\\d{14}$",
                "entity_type":      "EG_NATIONAL_ID",
                "recognizer_group": "structured",
                "presidio_score":   0.95,
            },
        },
    )

Persistence
-----------
    # Save to local YAML file
    overrides.to_yaml("my_config.yaml")

    # Load from YAML
    loaded = RegexOverrides.from_yaml("my_config.yaml")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class RegexOverrides:
    """
    Per-run regex catalog overrides.

    Attributes
    ----------
    add : dict[str, dict]
        Patterns to add (or replace if key already exists in CATALOG).
        Each value is a dict with PatternEntry fields:
        ``pattern``, ``entity_type``, ``recognizer_group`` (required);
        ``script``, ``presidio_score``, ``context_hints``,
        ``requires_validator``, ``requires_normalizer``,
        ``collision_group``, ``active`` (optional, with defaults).

    replace_all : bool
        If True, the default CATALOG is ignored entirely and only
        the patterns in ``add`` are used.  If False (default), the
        patterns in ``add`` are merged on top of the default CATALOG.
    """

    add: dict[str, dict] = field(default_factory=dict)
    remove: list[str] = field(default_factory=list)
    replace_all: bool = False

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize to a plain dict (JSON/YAML-safe)."""
        return {
            "replace_all": self.replace_all,
            "add": {
                name: _entry_to_dict(entry)
                for name, entry in self.add.items()
            },
            "remove": list(self.remove),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RegexOverrides":
        """Deserialize from a plain dict."""
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")
        return cls(
            replace_all=bool(data.get("replace_all", False)),
            add=dict(data.get("add", {})),
            remove=list(data.get("remove", [])),
        )

    # ── YAML persistence ─────────────────────────────────────────────────

    def to_yaml(self, path: str | Path) -> Path:
        """
        Save this configuration to a YAML file.

        Returns the resolved Path that was written.
        """
        import yaml

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                self.to_dict(), f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        return out.resolve()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RegexOverrides":
        """Load from a YAML file."""
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data or {})

    # ── Helpers ───────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        mode = "replace_all" if self.replace_all else "add_regex"
        return f"RegexOverrides(mode={mode}, patterns={len(self.add)})"

    def __bool__(self) -> bool:
        """True if there are any overrides to apply."""
        return bool(self.add) or bool(self.remove) or self.replace_all


def _entry_to_dict(entry: dict) -> dict:
    """Ensure all values in a pattern entry dict are YAML-serializable."""
    result = {}
    for k, v in entry.items():
        if isinstance(v, (tuple, frozenset)):
            result[k] = list(v)
        else:
            result[k] = v
    return result


# ─────────────────────────────────────────────────────────────────────────────
# RegexSet — the editable regex list, embedded directly in GlobalConfig
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegexSet:
    """
    The user-editable regex list that lives *inside* the global config.

    Unlike ``RegexOverrides`` (which is the engine-facing object consumed by
    the detector), ``RegexSet`` is a first-class, named, persistable list that
    the discovery/settings pages mutate (add / remove / modify / override a
    pattern) and then re-run a scan against.

    A ``RegexSet`` always knows how to produce the engine-facing
    ``RegexOverrides`` via :meth:`to_overrides`, keeping the equation/detector
    side fully decoupled from how the list is edited or stored.

    Attributes
    ----------
    name : Optional[str]
        Set when this list was loaded from / saved to a named config in the
        ConfigStore. ``None`` for an ad-hoc, in-session list.
    replace_all : bool
        If True, the default CATALOG is ignored and only ``patterns`` are used.
    patterns : dict[str, dict]
        Pattern entries keyed by name (same shape as ``RegexOverrides.add``).
    """

    name: Optional[str] = None
    replace_all: bool = False
    patterns: dict[str, dict] = field(default_factory=dict)

    # ── Mutation helpers (used by the discovery / settings layer) ──────────
    def add_pattern(self, key: str, entry: dict) -> "RegexSet":
        self.patterns[key] = entry
        return self

    def remove_pattern(self, key: str) -> bool:
        return self.patterns.pop(key, None) is not None

    def clear(self) -> "RegexSet":
        self.patterns = {}
        return self

    # ── Bridge to the engine-facing object ─────────────────────────────────
    def to_overrides(self) -> "RegexOverrides":
        """Produce the engine-facing RegexOverrides consumed by the detector."""
        return RegexOverrides(add=dict(self.patterns), replace_all=self.replace_all)

    @classmethod
    def from_overrides(
        cls, overrides: "RegexOverrides", name: Optional[str] = None
    ) -> "RegexSet":
        return cls(
            name=name,
            replace_all=overrides.replace_all,
            patterns=dict(overrides.add),
        )

    # ── Serialization ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "replace_all": self.replace_all,
            "patterns": {k: _entry_to_dict(v) for k, v in self.patterns.items()},
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RegexSet":
        if not data:
            return cls()
        # Accept both RegexSet shape ("patterns") and RegexOverrides shape ("add")
        patterns = data.get("patterns", data.get("add", {})) or {}
        return cls(
            name=data.get("name"),
            replace_all=bool(data.get("replace_all", False)),
            patterns=dict(patterns),
        )

    def __bool__(self) -> bool:
        return bool(self.patterns) or self.replace_all

    def __len__(self) -> int:
        return len(self.patterns)
