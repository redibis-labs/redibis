"""CLI: ``redibis steward …`` — reuse StewardReviewService; no extra writers.

Keep this module import-light: ``redibis --help`` must not pull scan / GE.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from redibis.cli.evidence_cmd import add_storage_flags, backend_from_args
from redibis.store.contract_store import ContractStore


def register_steward_commands(sub) -> None:
    p = sub.add_parser("steward", help="data-steward review of a synthesised contract")
    ssub = p.add_subparsers(dest="steward_action", required=True)

    def _t(parser):
        add_storage_flags(parser)
        parser.add_argument("table")

    p_ov = ssub.add_parser("overview", help="table + stats + guarantee state")
    _t(p_ov)

    p_col = ssub.add_parser("column", help="one column page")
    _t(p_col)
    p_col.add_argument("column")
    p_col.add_argument("--samples", action="store_true", help="request consented samples (role-gated)")
    p_col.add_argument(
        "--as-role", "--role", dest="role", default="explorer",
        help="role for sample gating (default explorer; pass --as-role admin to view consented samples)",
    )
    p_col.add_argument("--actor", default="cli")

    p_ver = ssub.add_parser("verdict", help="record a field verdict")
    _t(p_ver)
    p_ver.add_argument("column")
    p_ver.add_argument("--field", required=True)
    p_ver.add_argument("--decision", required=True,
                       choices=["accept", "reject", "needs_review", "no_action", "edit"])
    p_ver.add_argument("--source", dest="chosen_source", default="human")
    p_ver.add_argument("--run-id", dest="chosen_run_id", default="")
    p_ver.add_argument("--rationale", required=True)
    p_ver.add_argument("--rationale-text", default="")
    p_ver.add_argument("--value", default=None)
    p_ver.add_argument("--actor", default="cli")

    p_fin = ssub.add_parser("finalize", help="guarantee check and write A0–A5")
    _t(p_fin)
    p_fin.add_argument("--out-dir", help="also copy artifacts to a local directory")
    p_fin.add_argument("--actor", default="cli")

    p_ex = ssub.add_parser("export", help="download one artifact")
    _t(p_ex)
    p_ex.add_argument("--artifact", required=True,
                      choices=["verdicts", "evidence", "llm-context", "corpus", "graph", "contract"])
    p_ex.add_argument("-o", "--out", required=True)

    p_imp = ssub.add_parser("import-verdicts", help="re-apply a steward_verdicts.json package")
    _t(p_imp)
    p_imp.add_argument("package")
    p_imp.add_argument("--actor", default="cli")
    p_imp.add_argument("--reason", default="steward import")


def _svc(args):
    from redibis.services.steward_review_service import StewardReviewService
    backend = backend_from_args(args)
    bucket = getattr(args, "s3_contracts_bucket", "active-contracts")
    store = ContractStore(backend, bucket=bucket)
    return StewardReviewService(store), store


def run_steward(args) -> int:
    action = args.steward_action
    if action == "overview":
        svc, _ = _svc(args)
        print(json.dumps(svc.overview(args.table), indent=2, default=str))
        return 0
    if action == "column":
        svc, _ = _svc(args)
        print(json.dumps(svc.column(
            args.table, args.column,
            actor=getattr(args, "actor", "cli"),
            role=getattr(args, "role", "explorer"),
            include_samples=bool(getattr(args, "samples", False)),
        ), indent=2, default=str))
        return 0
    if action == "verdict":
        svc, _ = _svc(args)
        value = args.value
        if isinstance(value, str) and value[:1] in "{[":
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        payload = {
            "field": args.field,
            "decision": args.decision,
            "chosen_source": args.chosen_source,
            "chosen_run_id": args.chosen_run_id,
            "rationale_code": args.rationale,
            "rationale_text": args.rationale_text,
            "value": value,
        }
        print(json.dumps(svc.decide(args.table, args.column, args.field, payload, actor=args.actor),
                         indent=2, default=str))
        return 0
    if action == "finalize":
        svc, store = _svc(args)
        result = svc.finalize(args.table, actor=getattr(args, "actor", "cli"))
        print(json.dumps(result, indent=2, default=str))
        out_dir = getattr(args, "out_dir", None)
        if out_dir and result.get("ok"):
            dest = Path(out_dir)
            dest.mkdir(parents=True, exist_ok=True)
            listing = result.get("artifacts") or {}
            prefix = listing.get("prefix") or ""
            if prefix:
                for name in ("contract.reviewed.json", "steward_verdicts.json",
                             "review_evidence.json", "graph.jsonld"):
                    key = f"{prefix}/{name}"
                    if store.backend.exists(store.bucket, key):
                        data = store.backend.get_json(store.bucket, key)
                        (dest / name).write_text(
                            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
                        )
        return 0 if result.get("ok") else 1
    if action == "export":
        svc, _ = _svc(args)
        body, _ct, filename = svc.get_artifact(args.table, args.artifact)
        Path(args.out).write_bytes(body)
        print(f"Wrote {filename} to {args.out}")
        return 0
    if action == "import-verdicts":
        from redibis.review.verdict_package import load_verdict_package
        from redibis.services.evidence_review_service import EvidenceReviewService
        svc, store = _svc(args)
        raw = Path(args.package).read_text(encoding="utf-8")
        data = json.loads(raw)
        # Steward A1 generalises VerdictPackage; map pii entries onto the supplied path.
        entries = []
        for e in data.get("entries") or []:
            if e.get("field") not in (None, "pii", "column"):
                continue
            status = "pii" if (e.get("value") is True or (isinstance(e.get("value"), dict) and e["value"].get("is_pii"))) else "not_pii"
            if e.get("decision") in ("reject",):
                continue
            entries.append({
                "table": e.get("table") or args.table,
                "column": e.get("column"),
                "status": status,
                "entity_type": (e.get("value") or {}).get("entity_type") if isinstance(e.get("value"), dict) else None,
                "fingerprint_key": e.get("fingerprint_key") or "",
                "lifecycle_state": e.get("lifecycle_state") or "active",
                "decision_version": e.get("decision_version") or 1,
            })
        from redibis.review.verdict_package import VerdictEntry, VerdictPackage
        package = VerdictPackage(
            entries=[VerdictEntry.from_dict(e) for e in entries if e.get("column")],
            exporter=getattr(args, "actor", "cli"),
        )
        evid = EvidenceReviewService(store, pii_store=store.pii_decisions)
        result = evid.import_verdicts(
            args.table, bundle={"columns": {}}, package=package,
            actor=args.actor, reason=args.reason,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(f"unknown steward action: {action}", file=sys.stderr)
    return 2
