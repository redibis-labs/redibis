"""CLI handlers for ``redibis models`` (BYOM NER weights)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _resolve_models_base(args) -> str | None:
    """CLI models_dir: ``--models-dir`` → ``pii.models_dir`` from ``--config``."""
    explicit = getattr(args, "models_dir", None)
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    config_path = getattr(args, "config", None)
    if config_path:
        from redibis.pii.model_upload import models_dir_from_config

        base = models_dir_from_config(config_path)
        if base is not None:
            return str(base)
    return None


def run_models(args) -> int:
    action = args.models_action

    if action == "list":
        return _models_list(args)
    if action == "upload":
        return _models_upload(args)
    if action == "activate":
        return _models_activate(args)
    if action == "delete":
        return _models_delete(args)
    return 2


def _models_list(args) -> int:
    from redibis.services.model_service import list_models

    active = getattr(args, "active", None) or __import__("os").environ.get("REDIBIS_NER_MODEL", "")
    data = list_models(active_path=active, models_dir_base=_resolve_models_base(args))
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0
    print(f"models_dir: {data.get('models_dir')}")
    models = data.get("models") or []
    if not models:
        print("  (no models — upload with: redibis models upload ARCHIVE.zip [--name NAME])")
        return 0
    for m in models:
        mark = " *" if m.get("active") else ""
        labels = ", ".join((m.get("labels") or [])[:4])
        suffix = "…" if len(m.get("labels") or []) > 4 else ""
        print(f"  {m.get('name')}{mark}  [{m.get('type')}]  {m.get('path')}")
        if labels:
            print(f"      labels: {labels}{suffix}")
    return 0


def _models_upload(args) -> int:
    from redibis.services.model_service import ingest_upload

    path = Path(args.archive)
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 1
    result = ingest_upload(
        path.read_bytes(),
        path.name,
        name=getattr(args, "name", None),
        models_dir_base=_resolve_models_base(args),
    )
    if not result.ok:
        for err in result.errors:
            print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"ok: {result.name} → {result.path}")
    for warn in result.warnings:
        print(f"warning: {warn}", file=sys.stderr)
    return 0


def _models_activate(args) -> int:
    from redibis.config import RedibisConfig
    from redibis.services.model_service import activate_model

    try:
        path, meta = activate_model(args.name, models_dir_base=_resolve_models_base(args))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    config_path = getattr(args, "config", None)
    if config_path:
        cfg = RedibisConfig.from_yaml(config_path)
        cfg.pii.ner.model_path = path
        cfg.pii.ner.type = meta.get("type") or "gliner"
        if meta.get("labels"):
            cfg.pii.ner.labels = list(meta["labels"])
        cfg.to_yaml(config_path)
        print(f"Updated {config_path}: pii.ner.model_path = {path}")
        return 0

    print(f"Active model: {meta.get('name')} ({meta.get('type')})")
    print(f"  path: {path}")
    print("")
    print("Use for one scan:")
    print(f"  redibis scan data.csv db.table --ner-model {path}")
    print("")
    print("Or persist in shell / Docker:")
    print(f"  export REDIBIS_NER_MODEL={path}")
    print("")
    print("Or write into redibis.yaml:")
    print(f"  redibis models activate {args.name} --config redibis.yaml")
    return 0


def _models_delete(args) -> int:
    from redibis.services.model_service import delete_model

    if not getattr(args, "yes", False):
        try:
            answer = input(f"Delete model {args.name!r}? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("aborted")
            return 1

    try:
        result = delete_model(args.name, models_dir_base=_resolve_models_base(args))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"deleted: {result['name']} ({result['path']})")
    return 0
