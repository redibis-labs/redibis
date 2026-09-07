"""CLI for portable Redibis Packs (``.rdbpack``).

Enrichment packs keep ``redibis pack validate|inspect|evaluate``.
Portable config/behavior packs use this module:

  redibis rdbpack export-default [--out PATH]
  redibis rdbpack validate PATH
  redibis rdbpack import PATH [--dry-run] [--mode overlay|replace]
  redibis rdbpack diff PATH
  redibis rdbpack list
  redibis rdbpack remove ID@VERSION

``redibis pack export-default`` is an alias wired in ``pack_cmd``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from redibis import __version__ as REDIBIS_VERSION
from redibis.pack import load_pack
from redibis.pack.defaults import DEFAULT_PACK_ID, export_default_pack
from redibis.pack.errors import RdbPackError
from redibis.pack.stack import PackStackStore, diff_pack_against_active


def register_rdbpack_commands(sub) -> None:
    p = sub.add_parser(
        "rdbpack",
        help="portable Redibis Pack (.rdbpack) export / import / validate",
    )
    rsub = p.add_subparsers(dest="rdbpack_action", required=True)

    p_export = rsub.add_parser(
        "export-default",
        help="export shipped RedibisConfig defaults as redibis-default.rdbpack",
    )
    p_export.add_argument(
        "--out",
        default="redibis-default.rdbpack",
        help="output path (default: redibis-default.rdbpack)",
    )
    p_export.add_argument("--json", action="store_true", help="JSON summary")

    p_validate = rsub.add_parser("validate", help="validate a .rdbpack archive or folder")
    p_validate.add_argument("path", help="pack folder, .rdbpack ZIP, or tar.gz")
    p_validate.add_argument("--json", action="store_true", help="JSON output")

    p_import = rsub.add_parser("import", help="import a pack into the active stack")
    p_import.add_argument("path", help="pack folder or .rdbpack")
    p_import.add_argument("--dry-run", action="store_true", help="report only; do not apply")
    p_import.add_argument(
        "--mode",
        choices=("overlay", "replace"),
        default=None,
        help="override pack mode (default: pack.yaml mode)",
    )
    p_import.add_argument(
        "--activate",
        action="store_true",
        help="approve+activate imported behavior policies (default: draft only)",
    )
    p_import.add_argument("--json", action="store_true", help="JSON output")
    p_import.add_argument(
        "--stack-dir",
        help="pack stack directory (default: CONFIGS_DIR/packs or REDIBIS_PACK_STACK_DIR)",
    )

    p_diff = rsub.add_parser("diff", help="diff a pack's config against the active stack")
    p_diff.add_argument("path", help="pack folder or .rdbpack")
    p_diff.add_argument("--json", action="store_true", help="JSON output")
    p_diff.add_argument("--stack-dir", help="pack stack directory")

    p_list = rsub.add_parser("list", help="list active pack stack layers")
    p_list.add_argument("--json", action="store_true", help="JSON output")
    p_list.add_argument("--stack-dir", help="pack stack directory")

    p_remove = rsub.add_parser("remove", help="remove a layer from the active stack")
    p_remove.add_argument("identity", help="pack id@version")
    p_remove.add_argument("--json", action="store_true", help="JSON output")
    p_remove.add_argument("--stack-dir", help="pack stack directory")


def _store(args) -> PackStackStore:
    root = getattr(args, "stack_dir", None)
    return PackStackStore(root=Path(root)) if root else PackStackStore()


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif isinstance(payload, str):
        print(payload)
    else:
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def run_export_default(args) -> int:
    out = Path(getattr(args, "out", "redibis-default.rdbpack"))
    try:
        sha = export_default_pack(out)
    except RdbPackError as exc:
        print(f"rdbpack export-default failed: {exc}", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {
        "ok": True,
        "path": str(out),
        "id": DEFAULT_PACK_ID,
        "version": REDIBIS_VERSION,
        "pack_sha256": sha,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"wrote {out}")
        print(f"  identity: {DEFAULT_PACK_ID}@{REDIBIS_VERSION}")
        print(f"  sha256:   {sha}")
    return 0


def run_rdbpack_validate(args) -> int:
    path = getattr(args, "path", None)
    try:
        pack = load_pack(path)
    except RdbPackError as exc:
        print(f"rdbpack validate failed: {exc}", file=sys.stderr)
        return 2
    payload = {
        "ok": True,
        "identity": pack.manifest.identity,
        "pack_sha256": pack.pack_sha256,
        "mode": pack.manifest.mode,
        "contents": pack.manifest.contents.model_dump(mode="json"),
        "source_kind": pack.source_kind,
        "warnings": list(pack.warnings),
        "degraded": pack.degraded,
    }
    _emit(payload, getattr(args, "json", False))
    return 0


def run_rdbpack_import(args) -> int:
    try:
        report = _store(args).import_pack(
            args.path,
            mode=getattr(args, "mode", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            activate=bool(getattr(args, "activate", False)),
        )
    except RdbPackError as exc:
        print(f"rdbpack import failed: {exc}", file=sys.stderr)
        return 2
    _emit(report, getattr(args, "json", False))
    return 0


def run_rdbpack_diff(args) -> int:
    try:
        report = diff_pack_against_active(args.path, store=_store(args))
    except RdbPackError as exc:
        print(f"rdbpack diff failed: {exc}", file=sys.stderr)
        return 2
    _emit(report, getattr(args, "json", False))
    return 0


def run_rdbpack_list(args) -> int:
    store = _store(args)
    layers = [L.to_dict() for L in store.list_layers()]
    payload = {"layers": layers, "count": len(layers)}
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        if not layers:
            print("active pack stack: (empty)")
        else:
            print(f"active pack stack ({len(layers)} layers):")
            for layer in layers:
                print(
                    f"  - {layer['identity']}  sha256={layer['checksum'][:12]}…  "
                    f"mode={layer['mode']}"
                )
    return 0


def run_rdbpack_remove(args) -> int:
    try:
        report = _store(args).remove(args.identity)
    except RdbPackError as exc:
        print(f"rdbpack remove failed: {exc}", file=sys.stderr)
        return 2
    _emit(report, getattr(args, "json", False))
    return 0


def run_rdbpack(args) -> int:
    action = getattr(args, "rdbpack_action", None)
    dispatch = {
        "export-default": run_export_default,
        "validate": run_rdbpack_validate,
        "import": run_rdbpack_import,
        "diff": run_rdbpack_diff,
        "list": run_rdbpack_list,
        "remove": run_rdbpack_remove,
    }
    fn = dispatch.get(action)
    if fn is None:
        print(f"unknown rdbpack action: {action}", file=sys.stderr)
        return 2
    return fn(args)
