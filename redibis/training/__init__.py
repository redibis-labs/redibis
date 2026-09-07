"""Training dataset export + residency gates (OSS consume/export seam).

Public types are ``TrainingDataset`` / ``TrainingExample`` — not ``GoldenStore``
(fingerprint similarity). Trainers live under ``enterprise/addons/training/``.
"""

from __future__ import annotations

from redibis.training.dataset import TrainingDataset, TrainingExample
from redibis.training.exporter import ExportOptions, TrainingDatasetExporter
from redibis.training.portable_assert import PortableLeakError, assert_portable_row
from redibis.training.registry_hook import (
    JsonTableLearnedBackend,
    LearnedClassifierBackend,
    LocalLlmInferBackend,
    NullLearnedBackend,
    apply_learned_to_detections,
    learned_backend_from_config,
)
from redibis.training.residency import (
    ArtifactResidencyError,
    ArtifactResidencyGate,
    assert_artifact_portable,
)

__all__ = [
    "ArtifactResidencyError",
    "ArtifactResidencyGate",
    "ExportOptions",
    "JsonTableLearnedBackend",
    "LearnedClassifierBackend",
    "LocalLlmInferBackend",
    "NullLearnedBackend",
    "PortableLeakError",
    "TrainingDataset",
    "TrainingDatasetExporter",
    "TrainingExample",
    "apply_learned_to_detections",
    "assert_artifact_portable",
    "assert_portable_row",
    "learned_backend_from_config",
]
