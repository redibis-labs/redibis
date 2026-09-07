"""Free-text obfuscation preprocessing for the Text Gateway.

Deterministic expanders find spoken / spaced / parenthesized PII on the
original Unicode surface, canonicalize internally for validation, and emit
``Candidate`` spans that preserve source offsets for highlighting and de-id.
"""

from redibis.pii.text_preprocess.pipeline import ObfuscationPipeline, default_pipeline
from redibis.pii.text_preprocess.registry import (
    TextExpander,
    TextValidator,
    expander_registry,
    register_text_expander,
    resolve_expanders,
)
from redibis.pii.text_preprocess.surface import SurfaceSpan, ValidationOutcome

__all__ = [
    "ObfuscationPipeline",
    "default_pipeline",
    "TextExpander",
    "TextValidator",
    "expander_registry",
    "register_text_expander",
    "resolve_expanders",
    "SurfaceSpan",
    "ValidationOutcome",
]
