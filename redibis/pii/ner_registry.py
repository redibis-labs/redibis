"""
NER model discovery, manifest parsing, and cached backend loading.
"""

from __future__ import annotations

import json
import logging
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from redibis.pii.ner_backend import DEFAULT_NER_LABELS, GLiNERBackend, NERBackend

try:
    from redibis.pii.backends.http_ner import RemoteNERBackend
except ImportError:
    RemoteNERBackend = None  # type: ignore[misc, assignment]

if TYPE_CHECKING:
    from redibis.config import GlinerConfig, NERConfig, PIIConfig

logger = logging.getLogger("pii.ner.registry")

MANIFEST_FILENAME = "redibis-model.json"
GLINER_CONFIG_FILENAME = "gliner_config.json"


@dataclass
class NERModelSpec:
    type: str
    path: str
    name: str = ""
    labels: list[str] = field(default_factory=list)
    language: list[str] = field(default_factory=list)
    default_threshold: float | None = None


@dataclass
class NERModelRef:
    """Validated reference to one model entry in ``pii.ner.models``."""

    type: str
    path: str
    labels: list[str] = field(default_factory=list)
    threshold: float | None = None

    @classmethod
    def from_dict(cls, entry: dict) -> "NERModelRef | None":
        if not isinstance(entry, dict):
            return None
        path = (entry.get("path") or "").strip()
        if not path:
            return None
        raw_labels = entry.get("labels")
        labels = [str(x) for x in raw_labels] if isinstance(raw_labels, list) else []
        threshold = entry.get("threshold")
        return cls(
            type=str(entry.get("type") or "gliner").strip() or "gliner",
            path=path,
            labels=labels,
            threshold=float(threshold) if threshold is not None else None,
        )


@dataclass
class ManifestReport:
    ok: bool
    spec: NERModelSpec | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class NERModelRegistry:
    BACKENDS: dict[str, type] = {"gliner": GLiNERBackend}
    _cache: dict[tuple, NERBackend] = {}

    @classmethod
    def _backend_classes(cls) -> dict[str, type]:
        backends = dict(cls.BACKENDS)
        if RemoteNERBackend is not None:
            backends.setdefault("http", RemoteNERBackend)
        return backends

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()

    @classmethod
    def resolve_model_path(
        cls,
        ner: "NERConfig",
        gliner: "GlinerConfig | None" = None,
        *,
        models_dir: str | None = None,
    ) -> str | None:
        path = (ner.model_path or "").strip()
        if path:
            return path

        env_path = os.environ.get("REDIBIS_NER_MODEL", "").strip()
        if env_path:
            return env_path

        legacy_id = (gliner.model_id if gliner else "") or ""
        legacy_id = legacy_id.strip()
        if legacy_id:
            warnings.warn(
                "pii.gliner.model_id is deprecated; use pii.ner.model_path or REDIBIS_NER_MODEL",
                DeprecationWarning,
                stacklevel=3,
            )
            return legacy_id

        from redibis.pii.model_upload import resolve_models_dir

        root = resolve_models_dir(models_dir)
        specs = cls.discover(root)
        if len(specs) == 1:
            logger.info("Auto-selected sole NER model at %s", specs[0].path)
            return specs[0].path
        if len(specs) > 1:
            logger.info(
                "%d NER models under %s — activate one in Settings → NER Models "
                "or set pii.ner.model_path / REDIBIS_NER_MODEL",
                len(specs),
                root,
            )
        return None

    @classmethod
    def infer_type(cls, model_dir: Path) -> str:
        if (model_dir / GLINER_CONFIG_FILENAME).exists():
            return "gliner"
        if model_dir.is_dir() and any(model_dir.glob("*.safetensors")):
            return "gliner"
        return "unknown"

    @classmethod
    def read_manifest(cls, model_dir: Path) -> dict[str, Any] | None:
        manifest_path = model_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Invalid manifest at %s: %s", manifest_path, exc)
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def resolve_effective_labels(
        cls,
        *,
        stored: list[str] | None = None,
        model_path: str | None = None,
    ) -> tuple[list[str], str]:
        """Resolve NER labels: explicit config → model manifest → built-in defaults."""
        if isinstance(stored, list) and stored:
            return [str(x) for x in stored], "config"
        if model_path:
            path = Path(model_path)
            manifest = cls.read_manifest(path) if path.is_dir() else None
            raw_labels = (manifest or {}).get("labels")
            if isinstance(raw_labels, list) and raw_labels:
                return [str(x) for x in raw_labels], "manifest"
        return list(DEFAULT_NER_LABELS), "default"

    @classmethod
    def spec_from_path(
        cls,
        model_path: str,
        *,
        type_hint: str = "gliner",
        labels_override: list[str] | None = None,
    ) -> NERModelSpec:
        path = Path(model_path)
        manifest = cls.read_manifest(path) if path.is_dir() else None
        backend_type = (manifest or {}).get("type") or type_hint or cls.infer_type(path)
        labels = list(labels_override or [])
        if not labels:
            raw_labels = (manifest or {}).get("labels")
            if isinstance(raw_labels, list) and raw_labels:
                labels = [str(x) for x in raw_labels]
            else:
                labels = list(DEFAULT_NER_LABELS)
        name = str((manifest or {}).get("name") or path.name or model_path)
        language = (manifest or {}).get("language") or []
        if not isinstance(language, list):
            language = []
        threshold = (manifest or {}).get("default_threshold")
        return NERModelSpec(
            type=str(backend_type),
            path=model_path,
            name=name,
            labels=labels,
            language=[str(x) for x in language],
            default_threshold=float(threshold) if threshold is not None else None,
        )

    @classmethod
    def _instantiate_backend(
        cls,
        spec: NERModelSpec,
        *,
        device: str = "cpu",
        threshold: float = 0.3,
        batch_size: int = 8,
    ) -> NERBackend:
        backend_cls = cls._backend_classes().get(spec.type)
        if backend_cls is None:
            raise ValueError(f"unsupported NER backend type {spec.type!r}")
        effective_threshold = spec.default_threshold if spec.default_threshold is not None else threshold
        if spec.type == "http":
            return backend_cls(
                endpoint_url=spec.path,
                labels=spec.labels,
            )
        return backend_cls(
            spec.path,
            spec.labels,
            device=device,
            threshold=effective_threshold,
            batch_size=batch_size,
        )

    @classmethod
    def validate(cls, path: Path) -> ManifestReport:
        if not path.exists():
            return ManifestReport(ok=False, errors=[f"path does not exist: {path}"])
        spec = cls.spec_from_path(str(path))
        backend_cls = cls._backend_classes().get(spec.type)
        if backend_cls is None:
            return ManifestReport(
                ok=False,
                spec=spec,
                errors=[f"unsupported backend type {spec.type!r}"],
            )
        try:
            backend = cls._instantiate_backend(
                spec,
                threshold=spec.default_threshold or 0.3,
            )
        except (TypeError, ValueError) as exc:
            return ManifestReport(ok=False, spec=spec, errors=[str(exc)])
        health = backend.health_check()
        if not health.get("loadable"):
            return ManifestReport(
                ok=False,
                spec=spec,
                errors=[health.get("error", "model failed health check")],
            )
        return ManifestReport(ok=True, spec=spec)

    @classmethod
    def discover(cls, models_dir: Path) -> list[NERModelSpec]:
        if not models_dir.is_dir():
            return []
        specs: list[NERModelSpec] = []
        for child in sorted(models_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("_") or child.name.startswith("."):
                continue
            if (child / MANIFEST_FILENAME).exists() or (child / GLINER_CONFIG_FILENAME).exists():
                specs.append(cls.spec_from_path(str(child)))
        return specs

    @classmethod
    def load(cls, spec: NERModelSpec, *, device: str = "cpu", threshold: float = 0.3, batch_size: int = 8) -> NERBackend:
        effective_threshold = spec.default_threshold if spec.default_threshold is not None else threshold
        key = (spec.type, spec.path, device, effective_threshold, batch_size)
        cached = cls._cache.get(key)
        if cached is not None:
            return cached

        backend = cls._instantiate_backend(
            spec,
            device=device,
            threshold=effective_threshold,
            batch_size=batch_size,
        )
        cls._cache[key] = backend
        return backend

    @classmethod
    def load_many(
        cls,
        specs: list[NERModelSpec],
        *,
        device: str = "cpu",
        threshold: float = 0.3,
        batch_size: int = 8,
    ) -> list[NERBackend]:
        """Load multiple backends; skip specs that fail (isolated failures)."""
        backends: list[NERBackend] = []
        for spec in specs:
            try:
                backends.append(cls.load(
                    spec,
                    device=device,
                    threshold=threshold,
                    batch_size=batch_size,
                ))
            except Exception as exc:
                logger.warning("Skipping NER model %s (%s): %s", spec.name or spec.path, spec.type, exc)
        return backends

    @classmethod
    def try_load(
        cls,
        *,
        ner: "NERConfig",
        gliner: "GlinerConfig | None" = None,
        models_dir: str | None = None,
    ) -> NERBackend | None:
        model_path = cls.resolve_model_path(ner, gliner, models_dir=models_dir)
        if not model_path:
            logger.info("No NER model_path configured; NER engine disabled (regex-only)")
            return None
        spec = cls.spec_from_path(
            model_path,
            type_hint=ner.type,
            labels_override=ner.labels or None,
        )
        try:
            return cls.load(
                spec,
                device=ner.device,
                threshold=ner.threshold,
                batch_size=ner.batch_size,
            )
        except (ImportError, FileNotFoundError, OSError, ValueError) as exc:
            logger.warning("NER backend unavailable: %s", exc)
            return None

    @classmethod
    def try_load_pii(cls, pii: "PIIConfig") -> NERBackend | None:
        return cls.try_load(ner=pii.ner, gliner=pii.gliner)
