from __future__ import annotations
import logging

from redibis.pii.regex_catalog import (
    CATALOG, COLLISION_RESOLUTION_TABLE, catalog_validators,
    build_effective_catalog,
)

logger = logging.getLogger("pii.recognizer_factory")


def build_recognizers(
    group: str = "structured",
    arabic: bool = False,
    active_collision_suppressions: dict[str, str] | None = None,
    column_name_hints: tuple[str, ...] = (),
    regex_overrides: "RegexOverrides | None" = None,
):
    """
    Builds Presidio PatternRecognizer instances from the regex catalog.
    The catalog stays pure data; this factory is its only consumer.

    Parameters
    ----------
    group : "structured" | "free_text"
    arabic : If True, include patterns with script in ("arabic", "both").
             Set based on GE's Arabic character detection signal.
    active_collision_suppressions : dict mapping collision_group -> winning pattern_key.
             If provided, all other patterns in that collision_group are suppressed.
    column_name_hints : Column name tokens for context-based recognizer filtering.
    regex_overrides : RegexOverrides | None
             Per-run regex catalog overrides.  If provided, the effective
             catalog is computed via ``build_effective_catalog(overrides)``
             instead of using the default CATALOG directly.

    Returns
    -------
    list[PatternRecognizer]  — ready to pass to Presidio AnalyzerEngine.

    Example
    -------
    from redibis.pii.recognizer_factory import build_recognizers

    structured_recs = build_recognizers(group="structured", arabic=True)

    # With overrides:
    from redibis.pii.regex_overrides import RegexOverrides
    overrides = RegexOverrides(add={"my_pattern": {...}})
    recs = build_recognizers(group="structured", regex_overrides=overrides)
    """
    try:
        from presidio_analyzer import PatternRecognizer, Pattern
    except ImportError:
        raise ImportError(
            "presidio-analyzer is required. "
            "Install with: pip install presidio-analyzer"
        )

    class ValidatingPatternRecognizer(PatternRecognizer):
        """A custom Presidio Recognizer that hooks into the regex_catalog validators."""
        def __init__(
            self,
            validator_name: str | None,
            validated_score: float | None = None,
            **kwargs,
        ):
            super().__init__(**kwargs)
            self.validator_name = validator_name
            self._validated_score = validated_score

        def validate_result(self, pattern_text: str) -> float | None:
            if not self.validator_name:
                return None
            validator_fn = catalog_validators.get(self.validator_name)
            if not validator_fn:
                return None
            try:
                res = validator_fn(pattern_text)
                is_valid = res.get("valid", False) if isinstance(res, dict) else bool(res)
                if not is_valid:
                    return 0.0  # veto
                # None → keep regex score; float → promote (E1 fix).
                return self._validated_score
            except Exception as e:
                logger.warning(f"Validator {self.validator_name} exception: {e}")
                return 0.0

    # Build the effective catalog (applies overrides if provided)
    effective_catalog = build_effective_catalog(regex_overrides)

    suppressions = active_collision_suppressions or {}
    recognizers = []

    for name, entry in effective_catalog.items():
        if not entry.active:
            continue
        if entry.recognizer_group != group:
            continue
        if entry.script == "arabic" and not arabic:
            continue

        if entry.collision_group and entry.collision_group in suppressions:
            winner = suppressions[entry.collision_group]
            if winner != name:
                continue

        pattern = Pattern(
            name=name,
            regex=entry.pattern,
            score=entry.presidio_score,
        )
        rec = ValidatingPatternRecognizer(
            validator_name=entry.requires_validator,
            validated_score=getattr(entry, "validated_score", None),
            supported_entity=entry.entity_type,
            name=f"TelcoCatalog_{name}",
            patterns=[pattern],
            context=list(entry.context_hints),
        )
        recognizers.append(rec)

    return recognizers

