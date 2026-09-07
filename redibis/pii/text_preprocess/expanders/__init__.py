"""Built-in free-text expanders (import for registration side effects)."""

from redibis.pii.text_preprocess.expanders.age_phrase import AgePhraseExpander  # noqa: F401
from redibis.pii.text_preprocess.expanders.arabic_spoken_digits import (  # noqa: F401
    ArabicSpokenDigitsExpander,
)
from redibis.pii.text_preprocess.expanders.digit_cluster import DigitClusterExpander  # noqa: F401
from redibis.pii.text_preprocess.expanders.labeled_secret import LabeledSecretExpander  # noqa: F401
from redibis.pii.text_preprocess.expanders.parenthesized_digits import (  # noqa: F401
    ParenthesizedDigitsExpander,
)
from redibis.pii.text_preprocess.expanders.spaced_email import SpacedEmailExpander  # noqa: F401

__all__ = [
    "AgePhraseExpander",
    "ArabicSpokenDigitsExpander",
    "DigitClusterExpander",
    "LabeledSecretExpander",
    "ParenthesizedDigitsExpander",
    "SpacedEmailExpander",
]
