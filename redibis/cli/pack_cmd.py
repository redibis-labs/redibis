"""CLI for enrichment pack validate / inspect / evaluate + pack store."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from redibis.enrich.packs.errors import PackError
from redibis.enrich.packs.eval_engine import evaluate_pack_deltas
from redibis.enrich.packs.loader import load_enrichment_pack, pack_inspect_summary
from redibis.enrich.packs.validator import validate_loaded_pack

_STORE_ACTIONS = frozenset({"get", "list", "show", "history", "diff", "publish"})


def register_pack_commands(sub) -> None:
    p_pack = sub.add_parser(
        "pack",
        help="enrichment packs (validate/inspect/evaluate) and "
        "portable .rdbpack export-default",
    )
    pack_sub = p_pack.add_subparsers(dest="pack_action", required=True)

    p_validate = pack_sub.add_parser("validate", help="validate an enrichment pack")
    p_validate.add_argument("path", help="pack folder or .zip")
    p_validate.add_argument("--json", action="store_true", help="JSON output")

    p_inspect = pack_sub.add_parser("inspect", help="show pack metadata and counts")
    p_inspect.add_argument("path", help="pack folder or .zip")
    p_inspect.add_argument("--json", action="store_true", help="JSON output")
    p_inspect.add_argument(
        "--show-content",
        action="store_true",
        help="include local proprietary content (operator already has the pack)",
    )

    p_eval = pack_sub.add_parser(
        "evaluate",
        help="run deterministic golden/eval assertions (uses expected deltas offline "
        "unless --provider is given for live enrich)",
    )
    p_eval.add_argument("path", help="pack folder or .zip")
    p_eval.add_argument(
        "--provider",
        default="",
        help="optional live provider; default scores pack golden expected deltas offline",
    )
    p_eval.add_argument("--json", action="store_true", help="JSON output")

    # Portable Redibis Pack (design alias; full surface is ``redibis rdbpack``).
    p_export_default = pack_sub.add_parser(
        "export-default",
        help="export shipped RedibisConfig defaults as a portable .rdbpack",
    )
    p_export_default.add_argument(
        "--out",
        default="redibis-default.rdbpack",
        help="output path (default: redibis-default.rdbpack)",
    )
    p_export_default.add_argument("--json", action="store_true", help="JSON summary")

    from redibis.cli.evidence_cmd import add_storage_flags

    p_get = pack_sub.add_parser("get", help="download a published pack by UUID (sha256 verified)")
    p_get.add_argument("uuid", help="pack version UUID")
    p_get.add_argument("--out", default=".", help="directory to write {uuid}.zip")
    p_get.add_argument("--extract", action="store_true", help="extract the zip after download")
    add_storage_flags(p_get)

    p_list = pack_sub.add_parser("list", help="list published packs")
    p_list.add_argument("--family", help="filter by family_id")
    p_list.add_argument("--json", action="store_true")
    add_storage_flags(p_list)

    p_show = pack_sub.add_parser("show", help="show published pack manifest + contents")
    p_show.add_argument("uuid", help="pack version UUID")
    p_show.add_argument("--json", action="store_true")
    add_storage_flags(p_show)

    p_history = pack_sub.add_parser("history", help="version chain for a pack family")
    p_history.add_argument("--family", required=True, help="family_id (e.g. acme.policy.telecom)")
    p_history.add_argument("--json", action="store_true")
    add_storage_flags(p_history)

    p_diff = pack_sub.add_parser("diff", help="rule-level diff between two pack UUIDs")
    p_diff.add_argument("uuid_a", help="left pack UUID")
    p_diff.add_argument("uuid_b", help="right pack UUID")
    p_diff.add_argument("--json", action="store_true")
    add_storage_flags(p_diff)

    p_publish = pack_sub.add_parser("publish", help="publish a pack archive to the store")
    p_publish.add_argument("archive", help="path to .rdbpack / .zip / pack folder")
    p_publish.add_argument("--author", required=True, help="publisher identity")
    p_publish.add_argument("--json", action="store_true")
    add_storage_flags(p_publish)


def run_pack(args) -> int:
    action = getattr(args, "pack_action", None)
    if action == "export-default":
        from redibis.cli.rdbpack_cmd import run_export_default

        return run_export_default(args)
    if action in _STORE_ACTIONS:
        return _run_pack_store(args)

    path = getattr(args, "path", None)
    try:
        pack = load_enrichment_pack(path)
    except PackError as exc:
        print(f"pack load failed: {exc}", file=sys.stderr)
        return 2

    if action == "validate":
        report = validate_loaded_pack(pack)
        payload = report.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            status = "OK" if report.ok else "FAILED"
            print(f"pack validate: {status}")
            print(f"  identity: {report.identity}")
            print(f"  sha256:   {report.sha256}")
            print(f"  signature_status: {report.signature_status}")
            for err in report.errors:
                print(f"  error: {err}", file=sys.stderr)
            for warn in report.warnings:
                print(f"  warning: {warn}")
        return 0 if report.ok else 1

    if action == "inspect":
        report = validate_loaded_pack(pack)
        summary = pack_inspect_summary(pack)
        summary["validation_ok"] = report.ok
        summary["validation_errors"] = report.errors
        if getattr(args, "show_content", False):
            summary["content"] = {
                "glossary_canonical_names": [e.canonicalName for e in pack.glossary],
                "example_ids": [e.id for e in pack.manifest.examples],
                "eval_case_ids": [str(c.get("id")) for c in pack.eval_cases],
                "prompt_files": [
                    p
                    for p in (
                        pack.manifest.prompt.domain,
                        pack.manifest.prompt.terminology,
                        pack.manifest.prompt.edgeCases,
                    )
                    if p
                ],
            }
        if getattr(args, "json", False):
            print(json.dumps(summary, indent=2))
        else:
            print(yaml.safe_dump(summary, sort_keys=False, allow_unicode=True))
        return 0 if report.ok else 1

    if action == "evaluate":
        report = validate_loaded_pack(pack)
        if not report.ok:
            print("pack evaluate: validation failed", file=sys.stderr)
            for err in report.errors:
                print(f"  error: {err}", file=sys.stderr)
            return 1
        if getattr(args, "provider", ""):
            print(
                "live --provider evaluation is reserved; "
                "scoring golden expected deltas offline in v1",
                file=sys.stderr,
            )
        deltas: dict[str, dict[str, Any]] = {}
        # Map eval cases onto golden expected deltas when ids align; otherwise
        # load expected deltas by matching input file against golden examples.
        golden_by_input = {
            ex.input: pack.golden_deltas[ex.id]
            for ex in pack.manifest.examples
            if ex.id in pack.golden_deltas
        }
        for case in pack.eval_cases:
            case_id = str(case.get("id") or "")
            input_rel = str(case.get("input") or "")
            if case_id in pack.golden_deltas:
                deltas[case_id] = pack.golden_deltas[case_id]
            elif input_rel in golden_by_input:
                deltas[case_id] = golden_by_input[input_rel]
            else:
                # Try matching example id prefix
                for ex in pack.manifest.examples:
                    if case_id.startswith(ex.id) and ex.id in pack.golden_deltas:
                        deltas[case_id] = pack.golden_deltas[ex.id]
                        break
        result = evaluate_pack_deltas(pack.eval_cases, deltas)
        result["pack"] = pack.provenance()
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(yaml.safe_dump(result, sort_keys=False, allow_unicode=True))
        return 0 if result.get("ok") else 1

    print(f"unknown pack action: {action}", file=sys.stderr)
    return 2


def _pack_store_from_args(args):
    from redibis.cli.evidence_cmd import backend_from_args
    from redibis.store.pack_store import PackStore

    backend = backend_from_args(args)
    bucket = getattr(args, "s3_runs_bucket", "pii-reports")
    return PackStore(backend, bucket=bucket)


def _run_pack_store(args) -> int:
    from redibis.store.pack_store import PackStoreError, PackVersionCollisionError

    action = args.pack_action
    try:
        store = _pack_store_from_args(args)
    except Exception as exc:
        print(f"pack store: {exc}", file=sys.stderr)
        return 2

    if action == "list":
        refs = store.list(family_id=getattr(args, "family", None) or None)
        payload = [r.to_dict() for r in refs]
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            if not refs:
                print("(no published packs)")
                return 0
            print(f"{'uuid':<36}  {'kind':<8} {'id':<16} {'version':<8} family")
            for r in refs:
                print(
                    f"{r.uuid:<36}  {r.kind:<8} {r.id:<16} {r.version:<8} {r.family_id}"
                )
        return 0

    if action == "show":
        ref = store.head(args.uuid)
        if ref is None:
            print(f"unknown pack uuid: {args.uuid}", file=sys.stderr)
            return 1
        payload = ref.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
        return 0

    if action == "history":
        refs = store.history(args.family)
        payload = [r.to_dict() for r in refs]
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            if not refs:
                print(f"(no history for family {args.family})")
                return 0
            print(f"{'uuid':<36}  {'version':<8} parent_uuid")
            for r in refs:
                print(f"{r.uuid:<36}  {r.version:<8} {r.parent_uuid or '—'}")
        return 0

    if action == "publish":
        try:
            ref = store.publish(args.archive, author=args.author)
        except PackVersionCollisionError as exc:
            print(f"pack publish: {exc}", file=sys.stderr)
            return 2
        except PackStoreError as exc:
            print(f"pack publish: {exc}", file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(ref.to_dict(), indent=2))
        else:
            print(f"published {ref.uuid}  {ref.family_id}@{ref.version}")
        return 0

    if action == "get":
        import zipfile

        out_dir = Path(getattr(args, "out", ".") or ".")
        dest = out_dir / f"{args.uuid}.zip"
        extract_dir = out_dir / args.uuid
        try:
            data = store.get(args.uuid)
        except PackStoreError as exc:
            print(f"pack get: {exc}", file=sys.stderr)
            return 2
        out_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(str(dest))
        if getattr(args, "extract", False):
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest) as zf:
                zf.extractall(extract_dir)
            print(str(extract_dir))
        return 0

    if action == "diff":
        import tempfile

        from redibis.pack.diff import diff_pack_files, format_pack_diff
        from redibis.pack.archive import read_zip_files
        from redibis.pack.canonical import MANIFEST_NAME

        try:
            bytes_a = store.get(args.uuid_a)
            bytes_b = store.get(args.uuid_b)
        except PackStoreError as exc:
            print(f"pack diff: {exc}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory() as td:
            pa = Path(td) / "a.zip"
            pb = Path(td) / "b.zip"
            pa.write_bytes(bytes_a)
            pb.write_bytes(bytes_b)
            files_a = read_zip_files(pa, manifest_name=MANIFEST_NAME)
            files_b = read_zip_files(pb, manifest_name=MANIFEST_NAME)
        diff = diff_pack_files(files_a, files_b)
        if getattr(args, "json", False):
            print(json.dumps(diff, indent=2))
        else:
            print(format_pack_diff(diff, left_label=args.uuid_a, right_label=args.uuid_b))
        return 0

    print(f"unknown pack store action: {action}", file=sys.stderr)
    return 2
