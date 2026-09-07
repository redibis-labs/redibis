"""
Presidio NLP helpers — pattern-only regex path without spaCy.

redibis registers PatternRecognizers only; spaCy NER is unused. A no-op
NlpEngine avoids booting en_core_web_lg when presidio-analyzer is installed
without spaCy model wheels.
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from presidio_analyzer import RecognizerRegistry

logger = logging.getLogger("pii.presidio")


def _noop_nlp_engine():
    from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngine

    class _NoOpNlpEngine(NlpEngine):
        def load(self) -> None:
            pass

        def is_loaded(self) -> bool:
            return True

        def process_text(self, text: str, language: str) -> NlpArtifacts:
            return NlpArtifacts([], None, [], [], self, language)

        def process_batch(
            self,
            texts: Iterable[str],
            language: str,
            batch_size: int = 1,
            n_process: int = 1,
            **kwargs,
        ) -> Iterator[Tuple[str, NlpArtifacts]]:
            for text in texts:
                yield text, self.process_text(text, language)

        def is_stopword(self, word: str, language: str) -> bool:
            return False

        def is_punct(self, word: str, language: str) -> bool:
            return False

        def get_supported_entities(self) -> List[str]:
            return []

        def get_supported_languages(self) -> List[str]:
            return ["en"]

    return _NoOpNlpEngine()


def build_pattern_analyzer_engine(
    registry: "RecognizerRegistry",
    *,
    supported_languages: list[str] | None = None,
):
    """
    Build an AnalyzerEngine for pattern-only recognizers (no spaCy boot).

    Raises ImportError if presidio-analyzer is not installed.
    Raises RuntimeError if engine construction fails.
    """
    from presidio_analyzer import AnalyzerEngine

    langs = supported_languages or ["en"]
    return AnalyzerEngine(
        registry=registry,
        nlp_engine=_noop_nlp_engine(),
        supported_languages=langs,
    )
