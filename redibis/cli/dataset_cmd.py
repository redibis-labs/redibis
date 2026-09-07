"""CLI: ``redibis dataset export|stats`` — training corpus harvest."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

from redibis.training.dataset import TrainingDataset
from redibis.training.exporter import ExportOptions, TrainingDatasetExporter
from redibis.training.portable_assert import PortableLeakError
from redibis.training.residency import ArtifactResidencyError, ArtifactResidencyGate


def register_dataset_commands(sub) -> None:
    p = sub.add_parser(
        "dataset",
        help="export / inspect steward-reviewed training datasets",
    )
    dsub = p.add_subparsers(dest="dataset_action", required=True)

    p_export = dsub.add_parser(
        "export",
        help="export TrainingDataset JSONL (portable by default)",
    )
    p_export.add_argument(
        "-o", "--out",
        default="training_dataset.jsonl",
        help="output JSONL path (default: training_dataset.jsonl)",
    )
    p_export.add_argument("--table", action="append", dest="tables", help="limit to schema.table (repeatable)")
    p_export.add_argument("--database", help="limit to database prefix")
    p_export.add_argument(
        "--label-source",
        action="append",
        dest="label_sources",
        choices=("human_decision", "review", "contract_confirmed"),
        help="filter by label source (repeatable)",
    )
    p_export.add_argument("--min-confidence", type=float, help="min engine confidence from telemetry")
    p_export.add_argument(
        "--disagreements-only",
        action="store_true",
        help="only rows where derived engine baseline disagrees with the human label",
    )
    p_export.add_argument(
        "--include-samples",
        action="store_true",
        help="include cell samples / descriptions; forces residency=local",
    )
    p_export.add_argument(
        "--samples-from",
        help="CSV/Parquet with columns matching contract columns (required with --include-samples)",
    )
    p_export.add_argument("--max-samples", type=int, default=5, help="samples per column (default 5)")
    p_export.add_argument("--table-domain", default="", help="coarse non-identifying domain tag")
    p_export.add_argument("--json", action="store_true", help="JSON summary")
    p_export.add_argument("--config", help="redibis.yaml; or set REDIBIS_CONFIG")
    p_export.add_argument("--s3-endpoint", help="S3/MinIO endpoint URL")
    p_export.add_argument("--s3-contracts-bucket", default="active-contracts")
    p_export.add_argument(
        "--use-s3", action="store_true",
        help="use S3 storage (default: local under --output-dir/_dev_storage)",
    )
    p_export.add_argument("--output-dir", default="./reports")

    p_stats = dsub.add_parser("stats", help="summarize a TrainingDataset JSONL")
    p_stats.add_argument("path", help="path to .jsonl")
    p_stats.add_argument("--json", action="store_true", help="JSON output")
    p_stats.add_argument(
        "--check-portable",
        action="store_true",
        help="run assert_portable_row on every row (fails if any leak)",
    )
    p_stats.add_argument(
        "--check-residency",
        action="store_true",
        help="run ArtifactResidencyGate on the file (fails if local/raw_trained)",
    )


def _load_samples(path: str, *, max_samples: int) -> dict[str, list[str]]:
    """Return {column_name: [sample, …]} from CSV/Parquet."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"samples file not found: {path}")
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required for --samples-from") from exc

    if p.suffix.lower() in {".parquet", ".pq"}:
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)
    out: dict[str, list[str]] = {}
    for col in df.columns:
        series = df[col].dropna().astype(str)
        vals = [v for v in series.tolist() if v and v.lower() != "nan"]
        if vals:
            out[str(col)] = vals[: max(1, int(max_samples))]
    return out


def run_dataset(args) -> int:
    action = getattr(args, "dataset_action", None)
    if action == "export":
        return _cmd_export(args)
    if action == "stats":
        return _cmd_stats(args)
    print(f"unknown dataset action: {action}", file=sys.stderr)
    return 2


def _cmd_export(args) -> int:
    from redibis.cli.main import _build_backend, _contract_store

    if getattr(args, "include_samples", False) and not getattr(args, "samples_from", None):
        print(
            "dataset export: --include-samples requires --samples-from PATH "
            "(forces residency=local)",
            file=sys.stderr,
        )
        return 2

    samples_by_column: dict[str, list[str]] = {}
    if getattr(args, "include_samples", False):
        try:
            samples_by_column = _load_samples(
                args.samples_from,
                max_samples=int(getattr(args, "max_samples", 5) or 5),
            )
        except Exception as exc:
            print(f"dataset export: failed to load samples: {exc}", file=sys.stderr)
            return 2

    backend = _build_backend(args)
    store = _contract_store(args, backend)
    exporter = TrainingDatasetExporter(store)
    opts = ExportOptions(
        tables=getattr(args, "tables", None),
        database=getattr(args, "database", None),
        label_sources=getattr(args, "label_sources", None),
        min_confidence=getattr(args, "min_confidence", None),
        include_samples=bool(getattr(args, "include_samples", False)),
        samples_by_column=samples_by_column,
        max_samples_per_column=int(getattr(args, "max_samples", 5) or 5),
        table_domain=str(getattr(args, "table_domain", "") or ""),
        disagreements_only=bool(getattr(args, "disagreements_only", False)),
    )
    out = Path(getattr(args, "out", "training_dataset.jsonl"))
    try:
        ds = exporter.export_to_path(out, opts)
    except PortableLeakError as exc:
        print(f"dataset export refused (portable leak): {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"dataset export failed: {exc}", file=sys.stderr)
        return 2

    summary: dict[str, Any] = {
        "ok": True,
        "path": str(out),
        "residency": ds.residency,
        "checksum": ds.checksum,
        **ds.stats(),
        "audit": (ds.meta or {}).get("audit"),
    }
    if opts.include_samples:
        summary["note"] = (
            "residency=local — refused by pack export / ArtifactResidencyGate"
        )

    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"wrote {out}  residency={ds.residency}  rows={len(ds.examples)}")
        print(f"checksum {ds.checksum}")
        if opts.include_samples:
            print("NOTE: local corpus — do not pack or upload", file=sys.stderr)
    return 0


def _cmd_stats(args) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"not found: {path}", file=sys.stderr)
        return 2
    try:
        ds = TrainingDataset.from_jsonl(path)
    except Exception as exc:
        print(f"failed to load dataset: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "check_residency", False):
        try:
            ArtifactResidencyGate.check_path(path, label=str(path))
        except ArtifactResidencyError as exc:
            print(f"residency gate refused: {exc}", file=sys.stderr)
            for err in exc.errors:
                print(f"  - {err}", file=sys.stderr)
            return 2

    if getattr(args, "check_portable", False):
        from redibis.training.portable_assert import assert_portable_row

        try:
            for i, ex in enumerate(ds.examples):
                assert_portable_row(ex.to_dict(), path=f"row[{i}]")
        except PortableLeakError as exc:
            print(f"portable assert failed: {exc}", file=sys.stderr)
            return 2

    stats = ds.stats()
    if getattr(args, "json", False):
        print(json.dumps(stats, indent=2, default=str))
    else:
        print(yaml.safe_dump(stats, sort_keys=False, allow_unicode=True))
    return 0
