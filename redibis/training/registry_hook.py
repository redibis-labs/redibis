"""Consume seam for learned classifiers / local LLM infer (evidence only).

Structured classifiers populate ``PIIDetection.learned_*`` (never ``gliner_*``).
Generative local LLM backends emit classification proposals with ``suggest_only``.
Both no-op when disabled or when no model weights are present.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from redibis.models import PIIDetection
from redibis.training.features import build_column_features

PathLike = Union[str, Path]


class LearnedClassifierBackend(ABC):
    """Structured column classifier — returns evidence scores, never verdicts."""

    name: str = "learned"

    @abstractmethod
    def score_column(
        self,
        column: str,
        features: Mapping[str, Any],
        *,
        table: str = "",
    ) -> dict[str, Any]:
        """Return ``{score, label, entity}`` or empty when no signal."""

    def health_check(self) -> dict[str, Any]:
        return {"ok": True, "name": self.name, "loaded": False}


class NullLearnedBackend(LearnedClassifierBackend):
    """Default no-op backend (disabled / missing weights)."""

    name = "learned:null"

    def score_column(
        self,
        column: str,
        features: Mapping[str, Any],
        *,
        table: str = "",
    ) -> dict[str, Any]:
        return {"score": None, "label": None, "entity": None}

    def health_check(self) -> dict[str, Any]:
        return {"ok": True, "name": self.name, "loaded": False, "noop": True}


class JsonTableLearnedBackend(LearnedClassifierBackend):
    """Tiny portable backend: lookup scores from a JSON map (tests / stubs).

    File shape::

        {"telecom.customers.msisdn": {"score": 0.95, "label": "PHONE_NUMBER",
                                      "entity": "PHONE_NUMBER"}}
    """

    name = "learned:json"

    def __init__(self, path: PathLike):
        self.path = Path(path)
        self._table: dict[str, dict[str, Any]] = {}
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._table = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def score_column(
        self,
        column: str,
        features: Mapping[str, Any],
        *,
        table: str = "",
    ) -> dict[str, Any]:
        keys = []
        if table:
            keys.append(f"{table}.{column}")
        keys.append(column)
        for key in keys:
            hit = self._table.get(key)
            if hit:
                return {
                    "score": hit.get("score"),
                    "label": hit.get("label"),
                    "entity": hit.get("entity") or hit.get("label"),
                }
        return {"score": None, "label": None, "entity": None}

    def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "name": self.name,
            "loaded": bool(self._table),
            "path": str(self.path),
            "rows": len(self._table),
        }


class LocalLlmInferBackend:
    """Generative on-prem proposal backend — local residency only.

    Returns classification suggestion dicts for the ensemble (``suggest_only``).
    Refuses non-local residency tags. No-ops without a model.
    """

    name = "learned:local_llm"

    def __init__(
        self,
        *,
        residency: str = "local",
        model_path: str = "",
        raw_trained: bool = False,
    ):
        self.residency = str(residency or "local").lower()
        self.model_path = model_path
        self.raw_trained = bool(raw_trained)
        if self.residency not in {"local", "private"}:
            raise ValueError(
                f"LocalLlmInferBackend refuses non-local residency {self.residency!r}"
            )

    def propose(
        self,
        column: str,
        features: Mapping[str, Any],
        *,
        samples: Optional[Sequence[str]] = None,
        text_context: str = "",
        table: str = "",
    ) -> list[dict[str, Any]]:
        """Return suggest-only classification proposals (empty when no model)."""
        if not self.model_path or not Path(self.model_path).exists():
            return []
        # Phase 2: weights optional — real LoRA infer lands with enterprise trainer.
        # Stub returns nothing so OSS stays evidence-safe without weights.
        return []

    def health_check(self) -> dict[str, Any]:
        loaded = bool(self.model_path and Path(self.model_path).exists())
        return {
            "ok": True,
            "name": self.name,
            "loaded": loaded,
            "residency": self.residency,
            "raw_trained": self.raw_trained,
        }


def learned_backend_from_config(pii_config: Any = None) -> LearnedClassifierBackend:
    """Build a backend from ``PIIConfig.learned``; null when disabled / missing."""
    learned = getattr(pii_config, "learned", None) if pii_config is not None else None
    if learned is None or not getattr(learned, "enabled", False):
        return NullLearnedBackend()
    path = str(getattr(learned, "model_path", "") or "")
    kind = str(getattr(learned, "type", "gbt") or "gbt").lower()
    if not path:
        return NullLearnedBackend()
    p = Path(path)
    if kind in {"json", "stub"} or p.suffix.lower() == ".json":
        return JsonTableLearnedBackend(p)
    # Heavy GBT/encoder weights: enterprise trainer registers a real backend later.
    # Until then, treat missing torch backends as null.
    return NullLearnedBackend()


def apply_learned_to_detections(
    detections: Sequence[PIIDetection],
    *,
    backend: Optional[LearnedClassifierBackend] = None,
    table: str = "",
    props_by_column: Optional[Mapping[str, dict]] = None,
    telemetry_by_column: Optional[Mapping[str, dict]] = None,
) -> list[PIIDetection]:
    """Attach ``learned_*`` evidence to detections (``detected`` unchanged)."""
    backend = backend or NullLearnedBackend()
    props_by_column = props_by_column or {}
    telemetry_by_column = telemetry_by_column or {}
    out: list[PIIDetection] = []
    for det in detections:
        features = build_column_features(
            table=table,
            column=det.column,
            prop=props_by_column.get(det.column) or {"name": det.column},
            telemetry=telemetry_by_column.get(det.column) or {},
        )
        result = backend.score_column(det.column, features, table=table) or {}
        score = result.get("score")
        try:
            score_f = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_f = None
        if score_f is None:
            out.append(det)
            continue
        states = dict(det.engine_states or {})
        states["learned"] = {
            "enabled": True,
            "ran": True,
            "status": "matched",
            "score": score_f,
        }
        out.append(replace(
            det,
            learned_score=score_f,
            learned_label=str(result["label"]) if result.get("label") else None,
            learned_entity=str(result["entity"]) if result.get("entity") else None,
            learned_engine=getattr(backend, "name", "learned"),
            engine_states=states,
            detected=False,  # equation still decides
        ))
    return out


def classification_suggestions_from_learned(
    detections: Sequence[PIIDetection],
    *,
    domain: str = "DataSensitivity",
) -> dict[str, list[dict[str, Any]]]:
    """Map learned labels to ensemble suggestion dicts (per column)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for det in detections:
        if det.learned_score is None or not (det.learned_label or det.learned_entity):
            continue
        tag = str(det.learned_entity or det.learned_label)
        out[det.column] = [{
            "domain": domain,
            "tag": tag,
            "confidence": float(det.learned_score),
            "attributes": {},
        }]
    return out
