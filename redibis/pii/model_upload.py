"""
Secure NER model archive upload, quarantine extraction, and promotion.

Uploads land in ``{models_dir}/_quarantine/`` until ``validate()`` and smoke
inference pass; only then are they moved into ``{models_dir}/{name}/``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from redibis.pii.ner_registry import MANIFEST_FILENAME, NERModelRegistry

logger = logging.getLogger("pii.model_upload")

MAX_UPLOAD_BYTES = 5 * 1024 ** 3  # 5 GB
QUARANTINE_DIRNAME = "_quarantine"
SMOKE_STRINGS = ("Alice Smith", "+201001234567", "contact@example.com")
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


@dataclass
class UploadResult:
    ok: bool
    name: str = ""
    path: str = ""
    spec: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_models_dir(explicit: str | Path | None = None) -> Path:
    """Resolve models root without creating directories.

    Order: explicit arg → ``REDIBIS_MODELS_DIR`` → ``./models`` → ``/models``.
    Call ``_ensure_writable_dir`` before writes (upload / quarantine).
    """
    for candidate in (explicit, os.environ.get("REDIBIS_MODELS_DIR"), "./models", "/models"):
        if candidate is None:
            continue
        raw = str(candidate).strip()
        if raw:
            return Path(raw)
    return Path("/models")


def models_dir_from_config(config_path: str | Path | None) -> Path | None:
    """Return ``pii.models_dir`` only when explicitly set in the YAML file."""
    if not config_path:
        return None
    import yaml

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return None
    pii = raw.get("pii")
    if not isinstance(pii, dict) or "models_dir" not in pii:
        return None
    val = str(pii.get("models_dir") or "").strip()
    return Path(val) if val else None


def models_dir(base: str | Path | None = None) -> Path:
    return resolve_models_dir(base)


def quarantine_root(base: str | Path | None = None) -> Path:
    return models_dir(base) / QUARANTINE_DIRNAME


def _path_under_root(target: Path, root: Path) -> bool:
    root = root.resolve()
    target = target.resolve()
    return target == root or target.is_relative_to(root)


def _safe_extract_path(dest_root: Path, member_name: str) -> Path:
    if not member_name or member_name.startswith("/") or ".." in Path(member_name).parts:
        raise ValueError(f"unsafe archive path: {member_name!r}")
    dest_root = dest_root.resolve()
    target = (dest_root / member_name).resolve()
    if not _path_under_root(target, dest_root):
        raise ValueError(f"zip-slip rejected: {member_name!r}")
    return target


def _extract_budget_bytes(archive_size: int) -> int:
    return min(MAX_UPLOAD_BYTES, archive_size * 4)


def _check_disk_space(required_bytes: int, dest: Path) -> None:
    usage = shutil.disk_usage(dest)
    if usage.free < required_bytes:
        raise OSError(
            f"insufficient disk space: need {required_bytes} bytes, "
            f"only {usage.free} free under {dest}"
        )


def _scan_weight_warnings(model_dir: Path) -> list[str]:
    warnings: list[str] = []
    has_safetensors = any(model_dir.rglob("*.safetensors"))
    bin_files = list(model_dir.rglob("*.bin")) + list(model_dir.rglob("pytorch_model.bin"))
    pkl_files = list(model_dir.rglob("*.pkl")) + list(model_dir.rglob("*.pickle"))
    if pkl_files:
        warnings.append("archive contains pickle files; rejected for safety")
    if bin_files and not has_safetensors:
        warnings.append(
            "weights use legacy .bin format without accompanying .safetensors; "
            "prefer safetensors for untrusted uploads"
        )
    return warnings


def _resolve_model_root(extracted: Path) -> Path:
    """If archive unpacked to a single directory, treat that as the model root."""
    if not extracted.is_dir():
        raise ValueError("extracted upload is not a directory")
    children = [p for p in extracted.iterdir() if not p.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extracted


def _normalize_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or not _SAFE_NAME_RE.match(cleaned):
        raise ValueError(
            "model name must match [a-zA-Z0-9][a-zA-Z0-9._-]{0,127}"
        )
    return cleaned


def extract_archive(archive_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_size = archive_path.stat().st_size
    budget = _extract_budget_bytes(archive_size)
    extracted_bytes = 0
    suffix = archive_path.name.lower()
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                extracted_bytes += info.file_size
                if extracted_bytes > budget:
                    raise ValueError(
                        f"extracted content exceeds decompression budget ({budget} bytes)"
                    )
                target = _safe_extract_path(dest_dir, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return
    if suffix.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if suffix.endswith((".tar.gz", ".tgz")) else "r:"
        with tarfile.open(archive_path, mode) as tf:
            for member in tf.getmembers():
                if member.isdir():
                    continue
                extracted_bytes += member.size
                if extracted_bytes > budget:
                    raise ValueError(
                        f"extracted content exceeds decompression budget ({budget} bytes)"
                    )
                target = _safe_extract_path(dest_dir, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                with extracted as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return
    raise ValueError("unsupported archive type; use .zip, .tar.gz, or .tgz")


def smoke_inference(model_dir: Path) -> list[str]:
    """Run three short strings through the loaded backend."""
    report = NERModelRegistry.validate(model_dir)
    if not report.ok or report.spec is None:
        return list(report.errors)
    backend_cls = NERModelRegistry.BACKENDS.get(report.spec.type)
    if backend_cls is None:
        return [f"unsupported backend type {report.spec.type!r}"]
    backend = backend_cls(
        str(model_dir),
        report.spec.labels,
        threshold=report.spec.default_threshold or 0.3,
    )
    errors: list[str] = []
    for sample in SMOKE_STRINGS:
        try:
            backend.score_values([sample], "smoke_column")
        except Exception as exc:
            errors.append(f"smoke inference failed on {sample!r}: {exc}")
    return errors


def promote_quarantine(quarantine_dir: Path, name: str, *, base: str | Path | None = None) -> Path:
    name = _normalize_name(name)
    dest = models_dir(base) / name
    if dest.exists():
        raise FileExistsError(f"model {name!r} already exists at {dest}")
    shutil.move(str(quarantine_dir), str(dest))
    return dest


def _ensure_writable_dir(path: Path) -> None:
    """Raise ``OSError`` with a clear message when *path* is not writable."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(
            f"models directory is not writable: {path} ({exc}). "
            "Docker: mount /models read-write (remove :ro from compose). "
            "Or copy weights to ./models on the host and use Activate instead of upload."
        ) from exc


def ingest_upload(
    file_bytes: bytes,
    filename: str,
    *,
    name: str | None = None,
    models_dir_base: str | Path | None = None,
) -> UploadResult:
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return UploadResult(
            ok=False,
            errors=[f"upload exceeds size cap ({MAX_UPLOAD_BYTES} bytes)"],
        )
    if len(file_bytes) == 0:
        return UploadResult(ok=False, errors=["empty upload"])

    base = models_dir(models_dir_base)
    try:
        _ensure_writable_dir(base)
        _check_disk_space(len(file_bytes) * 2, base)
        _ensure_writable_dir(quarantine_root(models_dir_base))
    except OSError as exc:
        return UploadResult(ok=False, errors=[str(exc)])

    upload_id = uuid.uuid4().hex
    qdir = quarantine_root(models_dir_base) / upload_id
    qdir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=Path(filename or "upload.zip").suffix or ".zip",
            dir=qdir,
        ) as tmp:
            tmp.write(file_bytes)
            archive_path = Path(tmp.name)

        extract_dir = qdir / "extracted"
        extract_archive(archive_path, extract_dir)
        archive_path.unlink(missing_ok=True)

        model_root = _resolve_model_root(extract_dir)
        warnings = _scan_weight_warnings(model_root)
        if any("pickle" in w for w in warnings):
            return UploadResult(ok=False, errors=warnings, warnings=warnings)

        validation = NERModelRegistry.validate(model_root)
        if not validation.ok:
            return UploadResult(
                ok=False,
                errors=list(validation.errors),
                warnings=warnings + list(validation.warnings),
            )

        smoke_errors = smoke_inference(model_root)
        if smoke_errors:
            return UploadResult(ok=False, errors=smoke_errors, warnings=warnings)

        spec = validation.spec
        assert spec is not None
        final_name = _normalize_name(name or spec.name or Path(filename).stem)
        dest = promote_quarantine(model_root, final_name, base=models_dir_base)

        # If promotion moved a nested dir, model_root may differ — re-validate path
        if not dest.exists():
            return UploadResult(ok=False, errors=[f"promotion failed: {dest} missing"])

        NERModelRegistry.clear_cache()
        return UploadResult(
            ok=True,
            name=final_name,
            path=str(dest),
            spec={
                "type": spec.type,
                "name": spec.name,
                "labels": spec.labels,
                "language": spec.language,
                "default_threshold": spec.default_threshold,
                "path": str(dest),
            },
            warnings=warnings,
        )
    except Exception as exc:
        logger.warning("Model upload failed: %s", exc)
        return UploadResult(ok=False, errors=[str(exc)])
    finally:
        if qdir.exists():
            shutil.rmtree(qdir, ignore_errors=True)


def list_models(*, active_path: str = "", models_dir_base: str | Path | None = None) -> dict:
    base = models_dir(models_dir_base)
    specs = NERModelRegistry.discover(base)
    active_norm = str(Path(active_path).resolve()) if active_path else ""
    models = []
    for spec in specs:
        resolved = str(Path(spec.path).resolve())
        models.append({
            "name": spec.name,
            "type": spec.type,
            "path": spec.path,
            "labels": spec.labels,
            "language": spec.language,
            "default_threshold": spec.default_threshold,
            "active": bool(active_norm and resolved == active_norm),
        })
    return {
        "models_dir": str(base),
        "models": models,
        "active_path": active_path or None,
    }


def activate_model(name: str, *, models_dir_base: str | Path | None = None) -> tuple[str, dict]:
    name = _normalize_name(name)
    dest = models_dir(models_dir_base) / name
    if not dest.is_dir():
        raise FileNotFoundError(f"model {name!r} not found under {models_dir(models_dir_base)}")
    report = NERModelRegistry.validate(dest)
    if not report.ok:
        raise ValueError("; ".join(report.errors) or "model failed validation")
    NERModelRegistry.clear_cache()
    spec = report.spec
    assert spec is not None
    return str(dest.resolve()), {
        "name": name,
        "path": str(dest.resolve()),
        "type": spec.type,
        "labels": spec.labels,
    }


def delete_model(name: str, *, models_dir_base: str | Path | None = None) -> dict:
    """Remove a model directory under ``models_dir`` (not quarantine)."""
    name = _normalize_name(name)
    if name == QUARANTINE_DIRNAME or name.startswith("."):
        raise ValueError(f"cannot delete reserved name {name!r}")
    base = models_dir(models_dir_base).resolve()
    dest = (base / name).resolve()
    if not _path_under_root(dest, base) or dest == base:
        raise ValueError(f"unsafe model path for {name!r}")
    if not dest.is_dir():
        raise FileNotFoundError(f"model {name!r} not found under {base}")
    shutil.rmtree(dest)
    NERModelRegistry.clear_cache()
    return {"name": name, "deleted": True, "path": str(dest)}
