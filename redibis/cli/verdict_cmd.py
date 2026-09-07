"""CLI: ``redibis verdict export|preview|import`` — portable steward verdict packages.

Keep this module import-light: ``redibis --help`` must not pull GE / PII /
scan (``redibis.services`` eagerly imports the catalog/behavior/profiling
tree). ``EvidenceReviewService`` is only imported inside the handler
functions below, never at module scope.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from redibis.cli.evidence_cmd import add_storage_flags, backend_from_args
from redibis.store.contract_store import ContractStore


def register_verdict_commands(sub) -> None:
    p_verdict = sub.add_parser("verdict", help="steward verdict export and replay helpers")
    verdict_sub = p_verdict.add_subparsers(dest="verdict_action", required=True)

    p_export = verdict_sub.add_parser("export", help="export portable steward verdicts (JSON/JSONL)")
    add_storage_flags(p_export)
    p_export.add_argument("--table", help="single table (default: all tables with decisions)")
    p_export.add_argument("--out", default="-", help="output path, '-' for stdout, or .jsonl")
    p_export.add_argument("--jsonl", action="store_true", help="write JSONL instead of JSON")

    def _verdict_replay_common(p: argparse.ArgumentParser) -> None:
        add_storage_flags(p)
        p.add_argument("table")
        p.add_argument("--package", required=True, help="verdict package JSON/JSONL path")
        p.add_argument("--run-id", help="evidence run to classify against (default: latest)")

    p_preview = verdict_sub.add_parser(
        "preview",
        help="classify a verdict package's entries as matching/stale/missing/conflicting/invalid — never writes",
    )
    _verdict_replay_common(p_preview)

    p_import = verdict_sub.add_parser(
        "import", help="durably promote matching entries into the PII decision overlay",
    )
    _verdict_replay_common(p_import)
    p_import.add_argument("--actor", required=True, help="steward identity for the audit record")
    p_import.add_argument("--reason", required=True, help="why these verdicts are being imported")
    p_import.add_argument(
        "--merge-policy", choices=["matching_only", "overwrite_conflicts"], default="matching_only",
    )


def run_verdict_export(args) -> int:
    from redibis.services.evidence_review_service import EvidenceReviewService
    from redibis.store.review_store import ReviewStore

    backend = backend_from_args(args)
    bucket = getattr(args, "s3_contracts_bucket", "active-contracts")
    store = ContractStore(backend, bucket=bucket)
    svc = EvidenceReviewService(
        store,
        review_store=ReviewStore(backend, bucket),
        pii_store=store.pii_decisions,
    )
    tables = [args.table] if getattr(args, "table", None) else None
    try:
        payload = svc.export_verdicts(tables=tables, exporter="cli")
    except ValueError as exc:
        print(f"verdict export: {exc}", file=sys.stderr)
        return 2

    from redibis.review.verdict_package import VerdictPackage, VerdictEntry, write_verdict_package

    package = VerdictPackage(
        exported_at=str(payload.get("exported_at") or ""),
        exporter=str(payload.get("exporter") or "cli"),
        tables=list(payload.get("tables") or []),
        entries=[VerdictEntry.from_dict(e) for e in (payload.get("entries") or [])],
    )

    out = getattr(args, "out", "-") or "-"
    if out in ("-", ""):
        text = json.dumps(package.to_dict(), indent=2, sort_keys=True)
        print(text)
        return 0

    path = Path(out)
    as_jsonl = bool(getattr(args, "jsonl", False)) or path.suffix.lower() == ".jsonl"
    write_verdict_package(package, path, as_jsonl=as_jsonl)
    print(f"wrote {path}", file=sys.stderr)
    return 0


def _bundle_and_service(args):
    """Shared setup for ``verdict preview|import``: load the package, the
    target run's evidence bundle, and a service bound to the local
    ``ContractStore``. Returns ``(package, bundle, svc)``, or ``None`` after
    printing an error message.
    """
    from redibis.review.verdict_package import VerdictPackageError, load_verdict_package
    from redibis.scan.evidence_ops import EvidenceError, load_evidence_bundle
    from redibis.services.evidence_review_service import EvidenceReviewService
    from redibis.store.review_store import ReviewStore

    try:
        package = load_verdict_package(args.package)
    except VerdictPackageError as exc:
        print(f"verdict {args.verdict_action}: {exc}", file=sys.stderr)
        return None

    backend = backend_from_args(args)
    bucket = getattr(args, "s3_contracts_bucket", "active-contracts")
    store = ContractStore(backend, bucket=bucket)

    try:
        bundle, _hint = load_evidence_bundle(
            table=args.table,
            run_id=getattr(args, "run_id", None),
            latest=not getattr(args, "run_id", None),
            output_dir=Path(getattr(args, "output_dir", "./reports")),
            backend=backend,
            bucket=getattr(args, "s3_runs_bucket", "pii-reports"),
        )
    except EvidenceError as exc:
        print(f"verdict {args.verdict_action}: {exc}", file=sys.stderr)
        return None

    svc = EvidenceReviewService(
        store, review_store=ReviewStore(backend, bucket), pii_store=store.pii_decisions,
    )
    return package, bundle, svc


def run_verdict_preview(args) -> int:
    """``redibis verdict preview <table> --package PATH [--run-id ID]``.

    Classifies every entry as matching/stale/missing/conflicting/invalid
    against the selected run's evidence — never writes to any store. Shares
    the fingerprint-gated replay path with the REST
    ``/api/evidence/{table}/verdicts/preview`` endpoint and
    ``redibis.scan.evidence_ops``.
    """
    loaded = _bundle_and_service(args)
    if loaded is None:
        return 2
    package, bundle, svc = loaded
    preview = svc.preview_verdicts(args.table, bundle, package)
    print(json.dumps(preview, indent=2, default=str))
    return 0


def run_verdict_import(args) -> int:
    """``redibis verdict import <table> --package PATH --actor A --reason R``.

    Durably promotes matching (or, with ``--merge-policy
    overwrite_conflicts``, conflicting) entries into the PII decision
    overlay via ``PiiDecisionStore``/``ContractStore.set_pii_decision``, and
    records one audit event in ``ContractStore.verdict_import_log``. Never
    promotes stale, missing, or invalid entries.
    """
    loaded = _bundle_and_service(args)
    if loaded is None:
        return 2
    package, bundle, svc = loaded
    try:
        result = svc.import_verdicts(
            args.table, bundle, package,
            actor=args.actor, reason=args.reason, merge_policy=args.merge_policy,
        )
    except ValueError as exc:
        print(f"verdict import: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    print(f"imported {len(result.get('applied') or [])} verdict(s) for {args.table}", file=sys.stderr)
    return 0
