"""Multi-model NER ensemble — deep-scan / agentic only."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from redibis.pii.ner_backend import NERBackend, NERReport

if TYPE_CHECKING:
    from redibis.config import PIIConfig

logger = logging.getLogger("pii.ner.ensemble")


@dataclass
class NERPass:
    model: str
    labels: list[str]
    report: NERReport | None = None
    status: str = "ok"
    reason: str = ""


@dataclass
class NEREnsemble:
    """Run many backends × many label groups over one column sample."""

    backends: list[NERBackend]
    unavailable: list[dict] = field(default_factory=list)
    max_models: int = 4
    max_passes: int = 8

    def run_column(
        self,
        values: list[str],
        column_name: str,
        *,
        label_groups: list[list[str]] | None = None,
    ) -> list[NERPass]:
        groups: list[list[str] | None] = (
            [list(g) for g in label_groups] if label_groups else [None]
        )
        active_backends = self.backends[: max(0, self.max_models)]
        max_groups = max(1, len(groups))
        if active_backends and len(active_backends) * max_groups > self.max_passes:
            allowed_groups = max(1, self.max_passes // len(active_backends))
            if allowed_groups < len(groups):
                logger.warning(
                    "NER label_groups capped from %d to %d (max_passes=%d)",
                    len(groups),
                    allowed_groups,
                    self.max_passes,
                )
                groups = groups[:allowed_groups]

        out: list[NERPass] = []
        for backend in active_backends:
            for labels in groups:
                active = list(labels) if labels is not None else list(backend.labels)
                try:
                    report = backend.analyze(values, column_name, labels=labels)
                except Exception as exc:
                    logger.warning("NER analyze failed for %s: %s", backend.name, exc)
                    out.append(NERPass(
                        model=backend.name,
                        labels=active,
                        report=None,
                        status="unavailable",
                        reason=str(exc),
                    ))
                    continue
                out.append(NERPass(model=backend.name, labels=active, report=report))

        for entry in self.unavailable:
            model_name = str(entry.get("model") or "")
            reason = str(entry.get("reason") or "load failed")
            for labels in groups:
                active = list(labels) if labels is not None else []
                out.append(NERPass(
                    model=model_name,
                    labels=active,
                    report=None,
                    status="unavailable",
                    reason=reason,
                ))
        return out

    @classmethod
    def from_config(cls, pii_config: "PIIConfig", *, models_dir: str | None = None) -> "NEREnsemble":
        from redibis.pii.ner_registry import NERModelRef, NERModelRegistry, NERModelSpec

        ner = pii_config.ner
        backends: list[NERBackend] = []
        unavailable: list[dict] = []
        raw_models = ner.models or []
        if raw_models:
            for entry in raw_models:
                ref = NERModelRef.from_dict(entry)
                if ref is None:
                    continue
                model_name = f"{ref.type}:{ref.path}"
                try:
                    spec = NERModelRegistry.spec_from_path(
                        ref.path,
                        type_hint=ref.type or ner.type or "gliner",
                        labels_override=ref.labels or ner.labels or None,
                    )
                    if ref.threshold is not None:
                        spec = NERModelSpec(
                            type=spec.type,
                            path=spec.path,
                            name=spec.name,
                            labels=spec.labels,
                            language=spec.language,
                            default_threshold=ref.threshold,
                        )
                    backend = NERModelRegistry.load(
                        spec,
                        device=ner.device,
                        threshold=ner.threshold,
                        batch_size=ner.batch_size,
                    )
                    health = backend.health_check()
                    if not health.get("loadable"):
                        unavailable.append({
                            "model": backend.name,
                            "reason": health.get("error", "health check failed"),
                        })
                        continue
                    backends.append(backend)
                except Exception as exc:
                    logger.warning("Skipping NER model %s: %s", model_name, exc)
                    unavailable.append({"model": model_name, "reason": str(exc)})
        else:
            single = NERModelRegistry.try_load(
                ner=ner,
                gliner=pii_config.gliner,
                models_dir=models_dir or pii_config.models_dir,
            )
            if single is not None:
                backends = [single]
        return cls(
            backends,
            unavailable=unavailable,
            max_models=getattr(ner, "max_models", 4) or 4,
            max_passes=getattr(ner, "max_passes", 8) or 8,
        )
