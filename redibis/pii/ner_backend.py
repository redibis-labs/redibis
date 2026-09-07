"""
Pluggable NER backends for column-level PII evidence.

Heavy dependencies (gliner, torch) are imported lazily inside backend classes.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol, TypedDict, runtime_checkable

logger = logging.getLogger("pii.ner")

DEFAULT_NER_LABELS = [
    "person",
    "phone number",
    "email",
    "address",
    "national id",
    "passport",
    "credit card",
    # "organization" removed: a company name is not personal data.
]

# Canonical entity name → natural-language phrase for GLiNER inference.
_GLINER_PHRASE_BY_CANONICAL: dict[str, str] = {
    "PERSON": "person",
    "PHONE": "phone number",
    "PHONE_NUMBER": "phone number",
    "EMAIL": "email",
    "EMAIL_ADDRESS": "email",
    "ADDRESS": "address",
    "LOCATION": "address",
    "NATIONAL_ID": "national id",
    "PASSPORT": "passport",
    "CREDIT_CARD": "credit card",
    "ORG": "organization",
    "ORGANIZATION": "organization",
}


def entity_to_gliner_phrase(
    label: str,
    phrases: dict[str, str] | None = None,
) -> str:
    """Map a canonical (or legacy) label to a GLiNER natural-language phrase.

    Resolution order: ``phrases`` overlay → built-in map → heuristic.
    """
    key = label.strip().upper().replace(" ", "_")
    if phrases:
        if key in phrases:
            return phrases[key]
        if label.strip() in phrases:
            return phrases[label.strip()]
    mapped = _GLINER_PHRASE_BY_CANONICAL.get(key)
    if mapped:
        return mapped
    if " " in label.strip():
        return label.strip().lower()
    return key.lower().replace("_", " ")


def labels_to_gliner_phrases(
    labels: list[str],
    phrases: dict[str, str] | None = None,
) -> list[str]:
    """Translate a canonical label list to GLiNER phrases (deduped, order preserved)."""
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        phrase = entity_to_gliner_phrase(label, phrases=phrases)
        if phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def model_slug(name: str) -> str:
    """Filesystem-safe slug derived from ``backend.name``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class NERResult(TypedDict):
    score: float | None
    label: str | None
    match_rate: float | None


@dataclass
class NERHit:
    label: str
    score: float
    match_rate: float


@dataclass(frozen=True)
class NERSpan:
    """Single entity span from free-text NER (Unicode code-point offsets)."""

    start: int
    end: int
    label: str
    score: float
    text: str = ""
    model: str = ""


@dataclass
class NERReport:
    model: str
    labels_requested: list[str]
    hits: list[NERHit] = field(default_factory=list)

    def best(self) -> NERHit | None:
        if not self.hits:
            return None
        return max(self.hits, key=lambda h: h.score)

    def to_result(self) -> NERResult:
        best = self.best()
        if best is None:
            return {"score": None, "label": None, "match_rate": None}
        return {
            "score": best.score,
            "label": best.label,
            "match_rate": best.match_rate,
        }


@runtime_checkable
class NERBackend(Protocol):
    name: str
    labels: list[str]

    def analyze(
        self,
        values: list[str],
        column_name: str,
        *,
        labels: list[str] | None = None,
    ) -> NERReport: ...

    def score_values(
        self,
        values: list[str],
        column_name: str,
        *,
        labels: list[str] | None = None,
    ) -> NERResult: ...

    def analyze_text(
        self,
        text: str,
        *,
        labels: list[str] | None = None,
        phrases: dict[str, str] | None = None,
    ) -> list[NERSpan]: ...

    def health_check(self) -> dict: ...


class BaseNERBackend(ABC):
    """Backends implement ``analyze()``; optional ``analyze_text()`` for span path."""

    name: str
    labels: list[str]

    @abstractmethod
    def analyze(
        self,
        values: list[str],
        column_name: str,
        *,
        labels: list[str] | None = None,
    ) -> NERReport: ...

    def score_values(
        self,
        values: list[str],
        column_name: str,
        *,
        labels: list[str] | None = None,
    ) -> NERResult:
        return self.analyze(values, column_name, labels=labels).to_result()

    def analyze_text(
        self,
        text: str,
        *,
        labels: list[str] | None = None,
        phrases: dict[str, str] | None = None,
    ) -> list[NERSpan]:
        """Return per-entity spans for free-text scanning. Default: unsupported."""
        return []

    @abstractmethod
    def health_check(self) -> dict: ...


def _skip_value(val: object) -> bool:
    if not val or not isinstance(val, str):
        return True
    return val.upper() in ("NONE", "NAN", "N/A")


def _normalize_ner_label(raw_label: str) -> str:
    """Lowercase GLiNER-style label for back-compat with pre-analyze score_values."""
    return (raw_label or "").strip().lower()


def bundled_encoder_path(model_path: str | Path) -> Path | None:
    """Return ``{model}/encoder`` when a bundled HF encoder/tokenizer is present."""
    encoder = Path(model_path) / "encoder"
    if (encoder / "config.json").is_file():
        return encoder
    return None


@contextmanager
def _offline_gliner_config(model_path: Path) -> Iterator[None]:
    """Point ``gliner_config.json`` at a bundled ``encoder/`` dir for offline loads."""
    encoder = bundled_encoder_path(model_path)
    config_path = model_path / "gliner_config.json"
    if encoder is None or not config_path.is_file():
        yield
        return

    backup = config_path.read_text(encoding="utf-8")
    data = json.loads(backup)
    encoder_path = str(encoder.resolve())
    if data.get("model_name") == encoder_path:
        yield
        return

    data["model_name"] = encoder_path
    try:
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        yield
    finally:
        config_path.write_text(backup, encoding="utf-8")


class GLiNERBackend(BaseNERBackend):
    """Load GLiNER weights from a local path only (no HuggingFace hub download)."""

    def __init__(
        self,
        model_path: str,
        labels: list[str] | None = None,
        *,
        device: str = "cpu",
        threshold: float = 0.3,
        batch_size: int = 8,
    ) -> None:
        self._model_path = model_path
        self.labels = list(labels or DEFAULT_NER_LABELS)
        self._device = device
        self._threshold = threshold
        self._batch_size = max(1, batch_size)
        self._model = None

    @property
    def name(self) -> str:
        return f"gliner:{self._model_path}"

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            from gliner import GLiNER
        except ImportError as exc:
            # ``gliner`` itself may be installed but fail to import because a
            # transitive dependency (torch, onnxruntime) is broken in *this*
            # interpreter — that is a very different fix (repair the Python
            # env / use the venv that has a working torch) than "pip install
            # gliner". Surface the real underlying error instead of masking it.
            root_cause = str(exc)
            is_gliner_missing = exc.name == "gliner" if getattr(exc, "name", None) else (
                "no module named 'gliner'" in root_cause.lower()
            )
            if is_gliner_missing:
                raise ImportError(
                    "gliner is required for NER detection. Install with: "
                    "pip install redibis[ner-runtime]"
                ) from exc
            raise ImportError(
                f"gliner failed to import in this Python environment ({root_cause}). "
                "gliner itself appears to be installed, so this is likely a broken "
                "dependency (commonly torch/onnxruntime) in the interpreter running "
                "the webapp — not a missing package. Verify with: "
                "python -c 'import gliner' using the *exact* interpreter that "
                "started the server (check the startup log's 'python:' line), and "
                "reinstall torch there if it fails the same way."
            ) from exc

        try:
            model_path = Path(self._model_path)
            with _offline_gliner_config(model_path):
                model = GLiNER.from_pretrained(self._model_path, local_files_only=True)
        except OSError as exc:
            encoder = bundled_encoder_path(self._model_path)
            hint = (
                " Bundle the base tokenizer with "
                "./scripts/download_ner_model.sh --tokenizer-only "
                "(creates encoder/ beside the weights)."
                if encoder is None
                else ""
            )
            raise FileNotFoundError(
                f"NER model not found at {self._model_path!r}. "
                "Mount model weights under /models or set REDIBIS_NER_MODEL."
                f"{hint} Underlying error: {exc}"
            ) from exc

        if self._device and self._device != "cpu":
            try:
                model = model.to(self._device)
            except Exception as err:
                logger.warning("Could not move GLiNER model to %s: %s", self._device, err)
        self._model = model

    def analyze(
        self,
        values: list[str],
        column_name: str,
        *,
        labels: list[str] | None = None,
    ) -> NERReport:
        canonical_labels = list(labels or self.labels)
        gliner_labels = labels_to_gliner_phrases(canonical_labels)
        report = NERReport(model=self.name, labels_requested=canonical_labels)
        try:
            self._ensure_loaded()
        except (ImportError, FileNotFoundError) as exc:
            logger.warning("%s", exc)
            return report

        assert self._model is not None
        total = max(len(values), 1)
        # Aggregate by canonical entity key; store display label (lowercase) per key.
        agg: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "best_score": 0.0, "display_label": ""}
        )

        scored: list[tuple[str, str]] = []
        for val in values:
            if _skip_value(val):
                continue
            scored.append((val, f"{column_name}: {val}"))

        for i in range(0, len(scored), self._batch_size):
            batch = scored[i : i + self._batch_size]
            texts = [context_val for _, context_val in batch]
            try:
                batch_entities = self._model.batch_predict_entities(
                    texts,
                    gliner_labels,
                    threshold=self._threshold,
                )
            except Exception:
                continue
            for (_raw_val, _context_val), entities in zip(batch, batch_entities):
                if not entities:
                    continue
                seen_in_value: set[str] = set()
                for ent in entities:
                    raw = _normalize_ner_label(ent.get("label", ""))
                    if not raw:
                        continue
                    from redibis.models import canonical_entity

                    key = canonical_entity(raw.upper().replace(" ", "_")) or raw
                    if key in seen_in_value:
                        continue
                    seen_in_value.add(key)
                    bucket = agg[key]
                    bucket["count"] += 1
                    score = float(ent.get("score", 0) or 0)
                    if score > bucket["best_score"]:
                        bucket["best_score"] = score
                        bucket["display_label"] = raw

        for bucket in agg.values():
            if bucket["best_score"] <= 0:
                continue
            report.hits.append(NERHit(
                label=bucket["display_label"],
                score=round(bucket["best_score"], 4),
                match_rate=round(bucket["count"] / total, 4),
            ))
        report.hits.sort(key=lambda h: h.score, reverse=True)
        return report

    def analyze_text(
        self,
        text: str,
        *,
        labels: list[str] | None = None,
        phrases: dict[str, str] | None = None,
    ) -> list[NERSpan]:
        """GLiNER entity spans with code-point offsets into ``text`` (no column prefix)."""
        if not text or not isinstance(text, str):
            return []
        canonical_labels = list(labels or self.labels)
        gliner_labels = labels_to_gliner_phrases(canonical_labels, phrases=phrases)
        try:
            self._ensure_loaded()
        except (ImportError, FileNotFoundError) as exc:
            logger.warning("%s", exc)
            return []
        assert self._model is not None
        try:
            predict = getattr(self._model, "predict_entities", None)
            infer = getattr(self._model, "inference", None)
            if callable(predict):
                entities = predict(
                    text,
                    gliner_labels,
                    threshold=self._threshold,
                )
            elif callable(infer):
                batch = infer(
                    [text],
                    gliner_labels,
                    threshold=self._threshold,
                )
                entities = (batch or [None])[0] or []
            else:
                logger.warning(
                    "GLiNER model %s has no predict_entities/inference — cannot scan text",
                    self.name,
                )
                return []
        except Exception as exc:
            logger.warning("GLiNER predict_entities failed: %s", exc, exc_info=True)
            return []

        # phrase → canonical for reverse mapping when pack overlays French etc.
        reverse: dict[str, str] = {}
        for lab in canonical_labels:
            phrase = entity_to_gliner_phrase(lab, phrases=phrases)
            key = lab.strip().upper().replace(" ", "_")
            reverse[phrase.lower()] = key

        spans: list[NERSpan] = []
        for ent in entities or []:
            if not isinstance(ent, dict):
                continue
            raw = _normalize_ner_label(ent.get("label", ""))
            if not raw:
                continue
            try:
                start = int(ent.get("start"))
                end = int(ent.get("end"))
            except (TypeError, ValueError):
                continue
            if start < 0 or end > len(text) or start >= end:
                continue
            matched = text[start:end]
            score = float(ent.get("score", 0) or 0)
            mapped = reverse.get(raw) or raw
            spans.append(NERSpan(
                start=start,
                end=end,
                label=mapped,
                score=score,
                text=matched,
                model=self.name,
            ))
        return spans

    def health_check(self) -> dict:
        report: dict = {
            "type": "gliner",
            "path": self._model_path,
            "device": self._device,
            "loadable": False,
        }
        try:
            self._ensure_loaded()
            report["loadable"] = True
        except Exception as exc:
            report["error"] = str(exc)
        return report
