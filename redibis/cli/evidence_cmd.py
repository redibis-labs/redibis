"""CLI: ``redibis scan evidence|decide`` and ``redibis context build``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from redibis.store.storage_backend import LocalBackend, S3Config, StorageBackend, get_backend

# Keep this module import-light: ``redibis --help`` must not pull GE / PII / scan.
DECIDE_PRESET_CHOICES = ("audit", "investigation", "reporting")

_SCAN_NESTED = frozenset({"evidence", "decide"})
_GLOBAL_VALUE_FLAGS = frozenset({"--log-format"})
_STORAGE_VALUE_FLAGS = frozenset({
    "--s3-endpoint",
    "--s3-runs-bucket",
    "--s3-contracts-bucket",
    "--s3-pii-runs-bucket",
    "--s3-quality-runs-bucket",
    "--output-dir",
})


def add_storage_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--s3-endpoint", help="S3/MinIO endpoint URL")
    parser.add_argument("--s3-runs-bucket", default="pii-reports")
    parser.add_argument("--s3-contracts-bucket", default="active-contracts")
    parser.add_argument("--s3-pii-runs-bucket", default="pii-contracts")
    parser.add_argument("--s3-quality-runs-bucket", default="quality-contracts")
    parser.add_argument(
        "--use-s3", action="store_true",
        help="use S3 storage (default: local under --output-dir/_dev_storage)",
    )
    parser.add_argument("--output-dir", default="./reports")


def backend_from_args(args) -> StorageBackend:
    if not getattr(args, "use_s3", False):
        root = Path(getattr(args, "output_dir", "./reports")) / "_dev_storage"
        return LocalBackend(str(root))
    cfg = S3Config.from_env()
    if getattr(args, "s3_endpoint", None):
        cfg.endpoint_url = args.s3_endpoint
    return get_backend("s3", s3_config=cfg)


def _add_table_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--table", help="schema.table")
    run = parser.add_mutually_exclusive_group()
    run.add_argument("--run-id", dest="run_id", help="specific run id")
    run.add_argument(
        "--latest", action="store_true", default=False,
        help="use the newest evidence run (default when --run-id is omitted)",
    )


def _evidence_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redibis scan evidence",
        description="Emit, store, or export a per-table evidence bundle.",
    )
    add_storage_flags(p)
    _add_table_run_flags(p)
    p.add_argument(
        "evidence_action",
        nargs="?",
        choices=["store", "packs", "export", "coverage", "llm"],
        help="store | packs | export | coverage | llm (omit to emit the bundle)",
    )
    p.add_argument(
        "--out", default="-",
        help="write JSON/zip/dir to PATH, or '-' for stdout",
    )
    p.add_argument(
        "--list-runs", action="store_true",
        help="list run ids that have evidence for --table",
    )
    p.add_argument(
        "--all-tables", action="store_true",
        help="store: every table that has evidence",
    )
    p.add_argument(
        "--keep-masked-samples", action="store_true",
        help="store: mask sample/top-value literals instead of dropping them",
    )
    p.add_argument(
        "--download", action="store_true",
        help="packs: download every pack in the stack to --out",
    )
    p.add_argument(
        "--with-packs", action="store_true",
        help="export: include packs/{uuid}.zip for each stack pack",
    )
    p.add_argument("--call-id", help="llm: show one call by id")
    p.add_argument(
        "--raw", action="store_true",
        help="llm: print the restricted local copy (requires --actor and --reason)",
    )
    p.add_argument("--reason", help="required with --raw to access restricted LLM evidence")
    p.add_argument("--actor", help="required with --raw: steward identity for the audit record")
    p.add_argument(
        "--role",
        default="",
        help="steward role for --raw (defaults to the configured evidence.access.steward_role)",
    )
    return p


def _decide_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redibis scan decide",
        description="Replay PII verdicts at new thresholds without rescanning.",
    )
    add_storage_flags(p)
    _add_table_run_flags(p)
    p.add_argument(
        "--preset",
        required=True,
        choices=list(DECIDE_PRESET_CHOICES),
        help="investigation (recall) | reporting (balanced) | audit (precision)",
    )
    p.add_argument(
        "--equation",
        choices=["strict", "balanced", "lenient", "independent"],
        help="override the preset equation mode",
    )
    p.add_argument(
        "--set",
        dest="threshold_sets",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a threshold (repeatable), e.g. presidio_min=0.45",
    )
    p.add_argument("--out", help="write the replayed bundle JSON to PATH")
    p.add_argument(
        "--verdicts",
        help="prior portable verdict package (JSON/JSONL) for run-scoped overlay replay",
    )
    return p


def _context_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redibis context build",
        description="Assemble files into an LLM context envelope.",
    )
    p.add_argument(
        "--add",
        action="append",
        required=True,
        metavar="PATH",
        help="file or glob to include (repeatable)",
    )
    p.add_argument("--out", default="context.json", help="output JSON path, or '-' for stdout")
    p.add_argument("--max-bytes", type=int, default=400_000, dest="max_bytes")
    p.add_argument(
        "--allow-raw-pii", action="store_true",
        help="include bundles with sensitivity.contains_raw_pii=true",
    )
    p.add_argument("--reason", help="required when --allow-raw-pii is set")
    return p


def _skip_flag(argv: list[str], i: int, value_flags: frozenset[str]) -> int:
    tok = argv[i]
    name = tok.split("=", 1)[0]
    if name in value_flags and "=" not in tok:
        return i + 2
    return i + 1


def split_scan_nested(argv: list[str]) -> Optional[tuple[str, list[str]]]:
    """If argv is ``scan evidence|decide …``, return (action, rest)."""
    i = 0
    leading: list[str] = []
    while i < len(argv):
        tok = argv[i]
        if tok == "--debug":
            leading.append(tok)
            i += 1
            continue
        if tok.split("=", 1)[0] in _GLOBAL_VALUE_FLAGS:
            end = _skip_flag(argv, i, _GLOBAL_VALUE_FLAGS)
            leading.extend(argv[i:end])
            i = end
            continue
        break
    if i >= len(argv) or argv[i] != "scan":
        return None
    i += 1
    # Flags before the nested verb still belong to the nested parser.
    pre: list[str] = []
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help") and not pre:
            return None
        if tok.startswith("-"):
            end = _skip_flag(argv, i, _STORAGE_VALUE_FLAGS)
            pre.extend(argv[i:end])
            i = end
            continue
        break
    if i >= len(argv) or argv[i] not in _SCAN_NESTED:
        return None
    return argv[i], leading + pre + argv[i + 1 :]


def maybe_dispatch_scan_nested(argv: list[str]) -> Optional[int]:
    split = split_scan_nested(argv)
    if split is None:
        return None
    action, rest = split
    if action == "evidence":
        return run_scan_evidence(rest)
    if action == "decide":
        return run_scan_decide(rest)
    return None


def _load_for_args(args) -> tuple[dict, str, StorageBackend]:
    from redibis.scan.evidence_ops import load_evidence_bundle

    backend = backend_from_args(args)
    bucket = getattr(args, "s3_runs_bucket", "pii-reports")
    output_dir = Path(getattr(args, "output_dir", "./reports"))
    latest = bool(getattr(args, "latest", False)) or not getattr(args, "run_id", None)
    bundle, hint = load_evidence_bundle(
        table=args.table,
        run_id=getattr(args, "run_id", None),
        latest=latest,
        output_dir=output_dir,
        backend=backend,
        bucket=bucket,
    )
    return bundle, hint, backend


def _emit_json(payload: dict, out: str) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if out in ("-", "", None):
        print(text)
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)


def _pack_store(args, backend: StorageBackend):
    from redibis.store.pack_store import PackStore

    bucket = getattr(args, "s3_runs_bucket", "pii-reports")
    return PackStore(backend, bucket=bucket)


def run_scan_evidence(argv: list[str]) -> int:
    from redibis.scan.evidence_ops import EvidenceError, list_evidence_runs, load_evidence_bundle

    parser = _evidence_parser()
    args = parser.parse_args(argv)

    backend = backend_from_args(args)
    bucket = getattr(args, "s3_runs_bucket", "pii-reports")
    output_dir = Path(getattr(args, "output_dir", "./reports"))

    if not args.table and not (
        args.evidence_action == "store" and getattr(args, "all_tables", False)
    ):
        print("evidence: --table is required", file=sys.stderr)
        return 2

    if args.list_runs and not args.evidence_action:
        try:
            runs = list_evidence_runs(
                table=args.table,
                output_dir=output_dir,
                backend=backend,
                bucket=bucket,
            )
        except EvidenceError as exc:
            print(f"evidence: {exc}", file=sys.stderr)
            return 2
        if not runs:
            print(f"no evidence runs for {args.table}", file=sys.stderr)
            return 1
        for rid in runs:
            print(rid)
        return 0

    if args.evidence_action == "store":
        return _run_evidence_store(args, backend, bucket, output_dir)
    if args.evidence_action == "packs":
        return _run_evidence_packs(args, backend, bucket, output_dir)
    if args.evidence_action == "export":
        return _run_evidence_export(args, backend, bucket, output_dir)
    if args.evidence_action == "coverage":
        return _run_evidence_coverage(args, output_dir, backend, bucket)
    if args.evidence_action == "llm":
        return _run_evidence_llm(args, output_dir, backend, bucket)

    try:
        latest = bool(args.latest) or not args.run_id
        bundle, hint = load_evidence_bundle(
            table=args.table,
            run_id=args.run_id,
            latest=latest,
            output_dir=output_dir,
            backend=backend,
            bucket=bucket,
        )
    except EvidenceError as exc:
        print(f"evidence: {exc}", file=sys.stderr)
        return 2
    _emit_json(bundle, args.out)
    if args.out not in ("-", "", None):
        print(f"source={hint}", file=sys.stderr)
    return 0


def _run_evidence_store(args, backend, bucket, output_dir) -> int:
    from redibis.scan.evidence_ops import EvidenceError, load_evidence_bundle, store_evidence
    from redibis.store.run_output_writer import RunOutputWriter

    tables: list[str]
    if getattr(args, "all_tables", False):
        tables = _discover_tables(output_dir, backend, bucket)
        if args.table and args.table not in tables:
            tables.append(args.table)
        if not tables:
            print("evidence store: no tables with evidence found", file=sys.stderr)
            return 1
    else:
        if not args.table:
            print("evidence store: --table is required (or pass --all-tables)", file=sys.stderr)
            return 2
        tables = [args.table]

    any_ok = False
    for table in tables:
        try:
            latest = bool(getattr(args, "latest", False)) or not getattr(args, "run_id", None)
            bundle, hint = load_evidence_bundle(
                table=table,
                run_id=getattr(args, "run_id", None) if table == args.table else None,
                latest=True if table != args.table else latest,
                output_dir=output_dir,
                backend=backend,
                bucket=bucket,
            )
        except EvidenceError as exc:
            print(f"evidence store: {table}: {exc}", file=sys.stderr)
            continue
        run_id = (bundle.get("table") or {}).get("run_id") or getattr(args, "run_id", None) or "unknown"
        writer = RunOutputWriter(
            backend=backend,
            bucket=bucket,
            workflow="evidence",
            table=table,
            run_id=str(run_id),
        )
        try:
            stripped, key = store_evidence(
                bundle,
                writer,
                keep_masked_samples=bool(getattr(args, "keep_masked_samples", False)),
            )
        except Exception as exc:
            print(f"evidence store: {table}: {exc}", file=sys.stderr)
            return 2
        print(key)
        out = getattr(args, "out", None)
        if out and out != "-" and table == args.table:
            _emit_json(stripped, out)
        any_ok = True
        print(f"# stored from {hint}", file=sys.stderr)
    return 0 if any_ok else 1


def _discover_tables(output_dir: Path, backend, bucket: str) -> list[str]:
    from redibis.scan.evidence_ops import (
        EvidenceError,
        _bundle_table,
        _load_json_path,
        iter_stored_evidence_keys,
        list_local_evidence_paths,
    )

    tables: list[str] = []
    seen: set[str] = set()
    for path in list_local_evidence_paths(output_dir):
        try:
            name = _bundle_table(_load_json_path(path))
        except EvidenceError:
            continue
        if name and name not in seen:
            seen.add(name)
            tables.append(name)
    for key in iter_stored_evidence_keys(backend, bucket):
        # ``_meta/evidence/{table}/{run_id}.json`` — table may contain dots.
        rel = key.split("/", 2)[-1] if key.startswith("_meta/evidence/") else ""
        if "/" in rel:
            name = rel.rsplit("/", 1)[0]
            if name and name not in seen:
                seen.add(name)
                tables.append(name)
    return tables


def _run_evidence_packs(args, backend, bucket, output_dir) -> int:
    from redibis.scan.evidence_ops import (
        EvidenceError,
        format_pack_stack,
        load_evidence_bundle,
        pack_stack_from_bundle,
    )

    try:
        latest = bool(args.latest) or not args.run_id
        bundle, _hint = load_evidence_bundle(
            table=args.table,
            run_id=args.run_id,
            latest=latest,
            output_dir=output_dir,
            backend=backend,
            bucket=bucket,
        )
    except EvidenceError as exc:
        print(f"evidence packs: {exc}", file=sys.stderr)
        return 2

    store = _pack_store(args, backend)
    stack = pack_stack_from_bundle(bundle)
    local_uuids = set()
    for entry in stack.get("packs") or []:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("uuid") or "")
        if uid and store.head(uid) is not None:
            local_uuids.add(uid)
    print(format_pack_stack(bundle, local_uuids=local_uuids))

    if not getattr(args, "download", False):
        return 0
    out_raw = getattr(args, "out", None) or "./packs"
    if out_raw in ("-", ""):
        out_raw = "./packs"
    out_dir = Path(out_raw)
    out_dir.mkdir(parents=True, exist_ok=True)
    from redibis.store.pack_store import PackStoreError

    for entry in stack.get("packs") or []:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("uuid") or "").strip()
        if not uid:
            continue
        dest = out_dir / f"{uid}.zip"
        try:
            data = store.get(uid)
        except PackStoreError as exc:
            print(f"pack get {uid}: {exc}", file=sys.stderr)
            return 2
        dest.write_bytes(data)
        print(f"  wrote {dest}")
    return 0


def _run_evidence_export(args, backend, bucket, output_dir) -> int:
    from redibis.scan.evidence_ops import EvidenceError, export_with_packs, load_evidence_bundle

    if not args.out or args.out == "-":
        print("evidence export: --out PATH is required", file=sys.stderr)
        return 2
    try:
        latest = bool(args.latest) or not args.run_id
        bundle, _hint = load_evidence_bundle(
            table=args.table,
            run_id=args.run_id,
            latest=latest,
            output_dir=output_dir,
            backend=backend,
            bucket=bucket,
        )
    except EvidenceError as exc:
        print(f"evidence export: {exc}", file=sys.stderr)
        return 2
    store = _pack_store(args, backend) if getattr(args, "with_packs", False) else None
    try:
        path = export_with_packs(
            bundle,
            Path(args.out),
            pack_store=store,
            with_packs=bool(getattr(args, "with_packs", False)),
        )
    except Exception as exc:
        print(f"evidence export: {exc}", file=sys.stderr)
        return 2
    print(str(path))
    return 0


def run_scan_decide(argv: list[str]) -> int:
    from redibis.scan.evidence_ops import (
        EvidenceError,
        apply_supplied_verdicts,
        format_verdict_diff,
        parse_threshold_sets,
        replay_preset,
    )

    parser = _decide_parser()
    args = parser.parse_args(argv)
    if not args.table:
        print("decide: --table is required", file=sys.stderr)
        return 2
    try:
        bundle, _hint, _backend = _load_for_args(args)
        overrides = parse_threshold_sets(getattr(args, "threshold_sets", None) or [])
        replayed = replay_preset(
            bundle,
            preset=args.preset,
            equation=getattr(args, "equation", None),
            threshold_overrides=overrides or None,
        )
    except EvidenceError as exc:
        print(f"decide: {exc}", file=sys.stderr)
        return 2
    print(format_verdict_diff(bundle, replayed, preset=args.preset))

    verdicts_path = getattr(args, "verdicts", None)
    if verdicts_path:
        pii_decisions = None
        try:
            from redibis.store.contract_store import ContractStore
            from redibis.store.storage_backend import LocalBackend

            backend = backend_from_args(args)
            contracts_bucket = getattr(args, "s3_contracts_bucket", "active-contracts")
            store = ContractStore(backend, contracts_bucket)
            pii_decisions = store.get_pii_decisions(args.table)
        except Exception:
            pii_decisions = None
        try:
            review, warnings = apply_supplied_verdicts(
                replayed, verdicts_path, pii_decisions=pii_decisions,
            )
        except EvidenceError as exc:
            print(f"decide: verdict replay: {exc}", file=sys.stderr)
            return 2
        stale = warnings.get("stale") or []
        missing = warnings.get("missing") or []
        if stale:
            print(f"verdict replay: stale fingerprint (ignored): {', '.join(stale)}", file=sys.stderr)
        if missing:
            print(f"verdict replay: no matching verdict: {', '.join(missing)}", file=sys.stderr)
        applied = [
            c["column"] for c in review.get("columns", [])
            if c.get("effective_verdict", {}).get("source") == "supplied"
        ]
        if applied:
            print(f"verdict replay: applied to {len(applied)} column(s): {', '.join(applied)}")
        replayed = dict(replayed)
        replayed["steward_review"] = review
        replayed["verdict_replay_warnings"] = warnings

    if getattr(args, "out", None):
        _emit_json(replayed, args.out)
    return 0


def register_context_commands(sub) -> None:
    p_ctx = sub.add_parser("context", help="assemble files into an LLM context envelope")
    ctx_sub = p_ctx.add_subparsers(dest="context_action", required=True)
    p_build = ctx_sub.add_parser("build", help="build a context JSON from --add paths")
    p_build.add_argument(
        "--add", action="append", required=True, metavar="PATH",
        help="file or glob to include (repeatable)",
    )
    p_build.add_argument("--out", default="context.json", help="output JSON, or '-' for stdout")
    p_build.add_argument("--max-bytes", type=int, default=400_000, dest="max_bytes")
    p_build.add_argument("--allow-raw-pii", action="store_true")
    p_build.add_argument("--reason", help="required with --allow-raw-pii")


def _find_run_dir(output_dir: Path, run_id: str) -> Optional[Path]:
    direct = Path(output_dir) / run_id
    if direct.is_dir():
        return direct
    matches = list(Path(output_dir).rglob(run_id))
    for path in matches:
        if path.is_dir() and (
            (path / "evidence_manifest.json").is_file()
            or (path / "evidence_bundle.json").is_file()
        ):
            return path
    return None


def _configured_spool_and_policy(output_dir: Path | None = None):
    from redibis.evidence.spool import DEFAULT_SPOOL_DIR
    from redibis.telemetry.llm_evidence import resolve_spool_dir

    try:
        from redibis.config import RedibisConfig

        cfg = RedibisConfig.load()
    except Exception:
        cfg = None
    spool = resolve_spool_dir(cfg)
    policy = getattr(getattr(cfg, "evidence", None), "access", None) if cfg is not None else None
    if output_dir is not None:
        try:
            using_default = Path(spool).resolve() == Path(DEFAULT_SPOOL_DIR).resolve()
        except OSError:
            using_default = Path(spool) == Path(DEFAULT_SPOOL_DIR)
        if using_default:
            spool = Path(output_dir) / "_restricted_evidence"
    return spool, policy


def _find_stored_artifact(
    backend,
    bucket: str,
    table: str,
    run_id: str,
    name: str,
):
    from redibis.evidence.paths import WORKFLOW_PREFIXES, storage_prefix_for_key

    if backend is None or not run_id:
        return None
    table_safe = str(table).replace(".", "_")
    for workflow in WORKFLOW_PREFIXES:
        flat = f"{workflow}/{table_safe}/{run_id}/{name}"
        try:
            if backend.exists(bucket, flat):
                return backend.get_json(bucket, flat)
        except Exception:
            pass
        try:
            keys = backend.list_keys(bucket, prefix=f"{workflow}/{table_safe}/")
        except Exception:
            continue
        suffix = f"/{run_id}/{name}"
        for key in keys:
            if not key.endswith(suffix):
                continue
            if storage_prefix_for_key(key, run_id):
                try:
                    return backend.get_json(bucket, key)
                except Exception:
                    continue
    return None


def _run_evidence_coverage(args, output_dir: Path, backend=None, bucket: str = "pii-reports") -> int:
    from redibis.scan.evidence_ops import EvidenceError, load_evidence_bundle

    try:
        bundle, hint = load_evidence_bundle(
            table=args.table,
            run_id=getattr(args, "run_id", None),
            latest=bool(getattr(args, "latest", False)) or not getattr(args, "run_id", None),
            output_dir=output_dir,
            backend=backend,
            bucket=bucket,
        )
    except EvidenceError as exc:
        print(f"evidence coverage: {exc}", file=sys.stderr)
        return 2
    run_id = (bundle.get("table") or {}).get("run_id") or ""
    run_dir = _find_run_dir(Path(output_dir), str(run_id))
    man_path = (run_dir / "evidence_manifest.json") if run_dir else None
    payload = None
    if man_path and man_path.is_file():
        payload = json.loads(man_path.read_text(encoding="utf-8"))
    elif backend is not None and run_id:
        payload = _find_stored_artifact(
            backend, bucket, str(args.table), str(run_id), "evidence_manifest.json",
        )
    if payload is None:
        payload = {
            "table": args.table,
            "run_id": run_id,
            "source": hint,
            "coverage": {
                "profile": {"status": "evaluated" if bundle.get("columns") else "skipped"},
                "pii": {"status": "evaluated" if (bundle.get("table_summary") or {}).get("pii") else "skipped"},
            },
        }
    _emit_json(payload, getattr(args, "out", "-"))
    return 0


def _run_evidence_llm(args, output_dir: Path, backend=None, bucket: str = "pii-reports") -> int:
    from redibis.evidence.audit import (
        RestrictedAccessError,
        append_restricted_read,
        authorize_restricted_read,
        restricted_read_record,
    )
    from redibis.evidence.spool import llm_spool_dir
    from redibis.scan.evidence_ops import EvidenceError, load_evidence_bundle

    want_raw = bool(getattr(args, "raw", False))
    reason = (getattr(args, "reason", None) or "").strip()
    actor = (getattr(args, "actor", None) or "").strip()
    role = (getattr(args, "role", None) or "").strip()
    cfg_spool, policy = _configured_spool_and_policy(output_dir)
    audit_enabled = True if policy is None else bool(getattr(policy, "audit_enabled", True))
    if want_raw:
        try:
            role = authorize_restricted_read(
                actor=actor, reason=reason, role=role, policy=policy,
            )
        except RestrictedAccessError as exc:
            print(f"evidence llm: {exc}", file=sys.stderr)
            return int(getattr(exc, "code", 2) or 2)
    try:
        bundle, _hint = load_evidence_bundle(
            table=args.table,
            run_id=getattr(args, "run_id", None),
            latest=bool(getattr(args, "latest", False)) or not getattr(args, "run_id", None),
            output_dir=output_dir,
            backend=backend,
            bucket=bucket,
        )
    except EvidenceError as exc:
        print(f"evidence llm: {exc}", file=sys.stderr)
        return 2
    run_id = str((bundle.get("table") or {}).get("run_id") or "")
    run_dir = _find_run_dir(Path(output_dir), run_id)
    index = None
    index_root: Optional[Path] = None
    search_roots = [p for p in (
        run_dir,
        llm_spool_dir(cfg_spool, run_id).parent if run_id else None,
    ) if p]
    for root in search_roots:
        idx = Path(root) / "llm_calls" / "index.json"
        if idx.is_file():
            index = json.loads(idx.read_text(encoding="utf-8"))
            index_root = Path(root)
            break
    if index is None and backend is not None and not want_raw and run_id:
        index = _find_stored_artifact(
            backend, bucket, str(args.table), run_id, "llm_calls/index.json",
        )
    if index is None:
        print("evidence llm: no llm_calls/index.json", file=sys.stderr)
        return 1
    call_id = getattr(args, "call_id", None)

    def _audit(outcome: str, error: str = "", cid: str = "") -> Optional[Path]:
        if not want_raw:
            return Path(".")
        path = append_restricted_read(cfg_spool, restricted_read_record(
            actor=actor,
            reason=reason,
            call_id=str(cid or call_id or ""),
            run_id=run_id,
            table=str(args.table or ""),
            outcome=outcome,
            error=error,
            role=role,
        ))
        if audit_enabled and path is None:
            print("evidence llm: audit write failed; refusing restricted read", file=sys.stderr)
            return None
        return path if path is not None else Path(".")

    if not call_id:
        payload = index
        if not want_raw:
            payload = {
                "calls": [
                    {k: v for k, v in (c or {}).items() if k not in {"raw", "spool"}}
                    for c in (index.get("calls") or [])
                ],
                "warnings": list(index.get("warnings") or []),
            }
        elif _audit("ok") is None:
            return 1
        _emit_json(payload, getattr(args, "out", "-"))
        return 0

    cid = str(call_id)
    if cid in {"llm_prompt_context", "prompt-context", "llm_prompt_context.raw.json"}:
        path = None
        if run_id:
            spool_parent = llm_spool_dir(cfg_spool, run_id).parent
            for name in ("llm_prompt_context.raw.json", "llm_prompt_context.json"):
                cand = spool_parent / name
                if cand.is_file():
                    path = cand
                    break
        if path is None or not path.is_file():
            if _audit("missing", "restricted prompt-context not found locally", cid) is None:
                return 1
            print("evidence llm: no local restricted prompt-context", file=sys.stderr)
            return 1
        if want_raw:
            if _audit("ok", cid=cid) is None:
                return 1
            _emit_json(json.loads(path.read_text(encoding="utf-8")), getattr(args, "out", "-"))
            return 0
        print("evidence llm: prompt-context is restricted; pass --raw --actor --reason", file=sys.stderr)
        return 1

    for entry in index.get("calls") or []:
        if entry.get("call_id") != call_id:
            continue
        if want_raw:
            rel = entry.get("raw") or entry.get("spool") or ""
            path = None
            if rel:
                cand = Path(rel) if Path(rel).is_absolute() else (
                    (index_root / rel) if index_root is not None else None
                )
                if cand is not None and cand.is_file():
                    path = cand
            if path is None and run_id:
                spool_dir = llm_spool_dir(cfg_spool, run_id)
                matches = list(spool_dir.glob(f"*-{call_id}.raw.json")) if spool_dir.is_dir() else []
                if matches:
                    path = matches[0]
            outcome = "ok" if path is not None and path.is_file() else "missing"
            if _audit(outcome, "" if outcome == "ok" else "restricted file not found locally") is None:
                return 1
            if path is None or not path.is_file():
                print(f"evidence llm: no local restricted file for {call_id}", file=sys.stderr)
                return 1
            payload = json.loads(path.read_text(encoding="utf-8"))
            _emit_json(payload, getattr(args, "out", "-"))
            return 0
        rel = entry.get("shareable") or ""
        if rel and index_root is not None:
            path = Path(rel) if Path(rel).is_absolute() else index_root / rel
            if path.is_file():
                _emit_json(json.loads(path.read_text(encoding="utf-8")), getattr(args, "out", "-"))
                return 0
        if backend is not None and rel:
            payload = _find_stored_artifact(
                backend, bucket, str(args.table), run_id, str(rel),
            )
            if payload is not None:
                _emit_json(payload, getattr(args, "out", "-"))
                return 0
        print(f"evidence llm: no shareable file for {call_id}", file=sys.stderr)
        return 1
    print(f"evidence llm: call {call_id} not found", file=sys.stderr)
    return 1

def run_context(args) -> int:
    from redibis.scan.context_build import ContextBuildError, build_context, expand_add_paths

    if getattr(args, "context_action", None) != "build":
        print(f"unknown context action: {getattr(args, 'context_action', None)}", file=sys.stderr)
        return 2
    try:
        paths = expand_add_paths(args.add)
        envelope = build_context(
            paths,
            max_bytes=int(getattr(args, "max_bytes", 400_000)),
            allow_raw_pii=bool(getattr(args, "allow_raw_pii", False)),
            reason=getattr(args, "reason", None),
        )
    except ContextBuildError as exc:
        print(f"context build: {exc}", file=sys.stderr)
        return 2
    _emit_json(envelope, getattr(args, "out", "context.json"))
    return 0
