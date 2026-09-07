"""CLI for multi-domain classification policy engine."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from redibis.classification import (
    ClassificationService,
    JurisdictionContext,
    get_builtin_pack,
    list_builtin_packs,
    load_policy_pack,
)
from redibis.classification.atlas_bridge import atlas_classifications_preview
from redibis.services.catalog.mapping import load_contract_file, resolve_contract_table


def _load_config(args):
    import os
    from redibis.config import RedibisConfig

    path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
    if path:
        return RedibisConfig.from_yaml(path)
    return RedibisConfig.default()


def run_classification(args, store) -> int:
    if args.classification_action == "packs":
        packs = list_builtin_packs()
        if getattr(args, "json", False):
            print(json.dumps({"packs": packs}, indent=2))
        else:
            for name in packs:
                print(name)
        return 0

    policy = get_builtin_pack(getattr(args, "policy", None) or "telecom")
    if getattr(args, "policy_file", None):
        policy = load_policy_pack(Path(args.policy_file))

    svc = ClassificationService(policy)

    if args.classification_action == "describe-pack":
        info = {
            "name": policy.name,
            "version": policy.version,
            "description": policy.description,
            "domains": {d: policy.domain_tags(d) for d in policy.domains},
        }
        if getattr(args, "json", False):
            print(json.dumps(info, indent=2))
        else:
            print(yaml.safe_dump(info, sort_keys=False))
        return 0

    if args.classification_action == "classify":
        contract_path = Path(args.file)
        contract = load_contract_file(contract_path)
        table = resolve_contract_table(contract, override=getattr(args, "table", None))

        if getattr(args, "jurisdiction", None):
            svc.set_jurisdiction(JurisdictionContext(table=table, jurisdiction=args.jurisdiction))

        if getattr(args, "column", None):
            results = [svc.classify_column(contract, table, args.column)]
        else:
            results = svc.classify_contract(contract, table)

        payload = {
            "table": table,
            "policy": policy.name,
            "results": [
                {
                    "column": r.column,
                    "tags": r.tag_keys(),
                    "retention_days": r.retention_days,
                    "approval_role": r.approval_role,
                    "escalations": r.escalations,
                    "violations": r.violations,
                    "flags": r.flags,
                }
                for r in results
            ],
        }

        if getattr(args, "atlas_preview", False):
            payload["atlas"] = atlas_classifications_preview(results)

        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            for r in results:
                label = r.column or table
                print(f"{label}: {', '.join(r.tag_keys()) or '(none)'}")
                if r.escalations:
                    print(f"  ESCALATE: {'; '.join(r.escalations)}")
                if r.violations:
                    print(f"  VIOLATION: {'; '.join(r.violations)}")
        return 0

    if args.classification_action == "classify-active":
        table = args.table
        contract = store.get_active(table)
        if contract is None:
            print(f"No active contract for {table!r}", file=sys.stderr)
            return 1
        if getattr(args, "jurisdiction", None):
            svc.set_jurisdiction(JurisdictionContext(table=table, jurisdiction=args.jurisdiction))
        results = svc.classify_contract(contract, table)
        if getattr(args, "json", False):
            print(json.dumps({
                "table": table,
                "results": [{"column": r.column, "tags": r.tag_keys()} for r in results],
            }, indent=2))
        else:
            for r in results:
                print(f"{r.column}: {', '.join(r.tag_keys())}")
        return 0

    print(f"unknown classification action: {args.classification_action}", file=sys.stderr)
    return 2
