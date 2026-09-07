"""CLI: ``redibis contract synthesize`` — portable ODCS v3.1 Contract Synthesis."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def add_synthesize_parser(contract_sub) -> None:
    p = contract_sub.add_parser(
        "synthesize",
        help=(
            "Contract Synthesis: build a portable ODCS v3.1 candidate from an "
            "existing v3 contract + requirements + Spark/SQL/DataStage sources "
            "(does NOT write active Redibis contracts)"
        ),
    )
    p.add_argument(
        "--contract", "-f",
        required=True,
        help="Base ODCS v3 contract YAML/JSON (file path)",
    )
    p.add_argument(
        "--table",
        default="",
        help="Optional table name (documentation only; synthesis does not upsert)",
    )
    p.add_argument(
        "--requirements",
        nargs="*",
        default=[],
        help="Requirement document paths (.md/.yaml/.json/.txt)",
    )
    p.add_argument(
        "--source",
        nargs="*",
        default=[],
        help="Pipeline source paths (.sql/.py/.scala/.dsx/.xml) or ZIP archives",
    )
    p.add_argument(
        "--inputs",
        nargs="*",
        default=[],
        help="Mixed input paths (requirements, sources, or ZIP)",
    )
    p.add_argument(
        "--analysis-mode",
        choices=["deterministic", "assisted"],
        default="deterministic",
    )
    p.add_argument(
        "--odcs-version",
        default="v3.1.0",
        help="Target ODCS apiVersion for the portable export (default v3.1.0)",
    )
    p.add_argument(
        "--output-dir", "-o",
        default="./synthesis_out",
        help="Directory for portable artifacts (default ./synthesis_out)",
    )
    p.add_argument(
        "--provider",
        default="",
        help="LLM provider for assisted mode (from llm_providers.json)",
    )
    p.add_argument(
        "--compare-modes",
        action="store_true",
        help="Also run assisted comparison when a provider is available",
    )
    p.add_argument("--json", action="store_true", help="Print result summary as JSON")


def run_synthesize(args) -> int:
    from redibis.synthesis import ContractSynthesisRunner

    reqs = list(getattr(args, "requirements", None) or [])
    sources = list(getattr(args, "source", None) or [])
    mixed = list(getattr(args, "inputs", None) or [])
    # Mixed inputs go to both lists; ingest classifies by suffix.
    paths = mixed
    reqs = reqs + paths
    sources = sources + paths

    provider = None
    if (getattr(args, "analysis_mode", "") == "assisted"
            or getattr(args, "compare_modes", False)):
        provider_name = getattr(args, "provider", "") or "demo"
        try:
            from redibis.enrich.providers import get_provider
            provider = get_provider(provider_name)
        except Exception as exc:
            print(f"warning: could not load provider {provider_name!r}: {exc}", file=sys.stderr)
            if getattr(args, "analysis_mode", "") == "assisted":
                return 2

    runner = ContractSynthesisRunner(
        analysis_mode=getattr(args, "analysis_mode", "deterministic") or "deterministic",
        odcs_version=getattr(args, "odcs_version", "v3.1.0") or "v3.1.0",
        provider=provider,
    )
    try:
        result = runner.run(
            base_contract=args.contract,
            requirement_paths=reqs,
            source_paths=sources,
            output_dir=args.output_dir,
            also_run_assisted_compare=bool(getattr(args, "compare_modes", False)),
        )
    except Exception as exc:
        print(f"synthesis failed: {exc}", file=sys.stderr)
        return 1

    summary: dict[str, Any] = {
        "valid": result.valid,
        "errors": result.errors,
        "warnings": result.warnings,
        "apiVersion": (result.candidate or {}).get("apiVersion"),
        "version": (result.candidate or {}).get("version"),
        "artifacts": result.artifacts,
        "traceability": (result.traceability or {}).get("coverage"),
        "writes_active_contract": False,
    }
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2, default=str))
    else:
        status = "VALID" if result.valid else "INVALID"
        print(f"Contract Synthesis [{status}] → {args.output_dir}")
        print(f"  apiVersion: {(result.candidate or {}).get('apiVersion')}")
        print(f"  version:    {(result.candidate or {}).get('version')}")
        cov = (result.traceability or {}).get("coverage") or {}
        if cov:
            print(f"  coverage:   {cov}")
        for name, path in (result.artifacts or {}).items():
            print(f"  artifact:   {name} → {path}")
        if result.errors:
            print("  errors:")
            for e in result.errors[:20]:
                print(f"    - {e}")
        if result.warnings:
            print("  warnings:")
            for w in result.warnings[:10]:
                print(f"    - {w}")
        print("  note: portable export only — does not call ContractStore.upsert()")
    return 0 if result.valid else 1
