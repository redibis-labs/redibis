"""CLI commands for golden vector store and structural fingerprints."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from redibis.profiling.fingerprint import ColumnFingerprint, compute_table_fingerprints
from redibis.profiling.inference import infer_column_governance
from redibis.profiling.vector_store import VectorConfig, get_vector_store, reset_vector_store
from redibis.profiling.vectorize import fingerprint_to_vector
from redibis.store.fingerprint_store import FingerprintStore
from redibis.store.golden_store import GoldenStore
from redibis.store.storage_backend import get_backend

log = logging.getLogger(__name__)


def register_golden_commands(sub) -> None:
    p_golden = sub.add_parser("golden", help="golden reference column set (structural similarity)")
    golden_sub = p_golden.add_subparsers(dest="golden_action", required=True)

    p_load = golden_sub.add_parser("load", help="hydrate vector index from golden path")
    p_load.add_argument("--source", choices=["local", "minio"], default="local")
    p_load.add_argument("--json", action="store_true")
    p_load.set_defaults(func=_cmd_golden_load)

    p_list = golden_sub.add_parser("list", help="list golden columns")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_golden_list)

    p_remove = golden_sub.add_parser("remove", help="remove a golden column")
    p_remove.add_argument("table_column", help="table.column")
    p_remove.set_defaults(func=_cmd_golden_remove)

    p_reindex = golden_sub.add_parser("reindex", help="rebuild vector index from golden store")
    p_reindex.set_defaults(func=_cmd_golden_reindex)

    p_import = golden_sub.add_parser("import-runs", help="seed golden set from local approved runs")
    p_import.add_argument("--runs-dir", default="./reports")
    p_import.set_defaults(func=_cmd_golden_import_runs)

    p_vector = sub.add_parser("vector", help="golden vector store status")
    vector_sub = p_vector.add_subparsers(dest="vector_action", required=True)
    p_vstatus = vector_sub.add_parser("status", help="show active backend health")
    p_vstatus.add_argument("--json", action="store_true")
    p_vstatus.set_defaults(func=_cmd_vector_status)

    p_similar = sub.add_parser("similar", help="find similar golden columns")
    p_similar.add_argument("table_column", help="table.column")
    p_similar.add_argument("--k", type=int, default=5)
    p_similar.add_argument("--json", action="store_true")
    p_similar.set_defaults(func=_cmd_similar)


def _parse_table_column(spec: str) -> tuple[str, str]:
    if "." not in spec:
        raise SystemExit(f"expected table.column, got {spec!r}")
    table, col = spec.rsplit(".", 1)
    return table, col


def _cmd_golden_load(args) -> int:
    reset_vector_store()
    store = get_vector_store(force_rebuild=True)
    count = store.load()
    out = {"loaded": count, "health": store.health()}
    print(json.dumps(out, indent=2) if args.json else f"Loaded {count} golden column(s)")
    return 0


def _cmd_golden_list(args) -> int:
    backend = get_backend()
    gs = GoldenStore.from_env(backend, "active-contracts")
    cols = gs.list_all()
    if args.json:
        print(json.dumps([c.to_dict() for c in cols], indent=2))
    else:
        for c in cols:
            print(f"{c.table_name}.{c.column_name}  {c.classification or '-'}  {c.entity_type or '-'}")
    return 0


def _cmd_golden_remove(args) -> int:
    table, col = _parse_table_column(args.table_column)
    store = get_vector_store()
    ok = store.delete(table, col)
    print("removed" if ok else "not found")
    return 0 if ok else 1


def _cmd_golden_reindex(args) -> int:
    return _cmd_golden_load(args)


def _cmd_golden_import_runs(args) -> int:
    """Bootstrap golden set from local run dirs with approved columns."""
    from redibis.profiling.golden_writer import _PROMOTION_KINDS, golden_from_contract_prop

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        raise SystemExit(f"runs dir not found: {runs_dir}")
    store = get_vector_store()
    batch: list = []
    for session_file in runs_dir.rglob("session.json"):
        try:
            data = json.loads(session_file.read_text())
        except Exception:
            continue
        approved = (data.get("approved") or {}).get("items") or []
        table = data.get("table_name", "")
        for item in approved:
            if item.get("kind") not in _PROMOTION_KINDS:
                continue
            if item.get("status") not in ("approved", "merged"):
                continue
            col = item.get("column")
            if not col or col == "__table__":
                continue
            fp_raw = None
            fp_dir = session_file.parent / "fingerprints"
            if fp_dir.is_dir():
                fp_path = fp_dir / f"{col}.json"
                if fp_path.exists():
                    fp_raw = json.loads(fp_path.read_text())
            golden = golden_from_contract_prop(
                table, col, item.get("payload") or {"name": col}, fingerprint=fp_raw,
            )
            if golden is not None:
                batch.append(golden)
    if batch:
        store.upsert_many(batch)
    print(f"imported {len(batch)} golden column(s)")
    return 0


def _cmd_vector_status(args) -> int:
    store = get_vector_store()
    out = store.health()
    print(json.dumps(out, indent=2) if args.json else json.dumps(out))
    return 0


def _cmd_similar(args) -> int:
    table, col = _parse_table_column(args.table_column)
    backend = get_backend()
    fp_store = FingerprintStore.from_env(backend, "active-contracts")
    raw = fp_store.get_column(table, col)
    if not raw:
        raise SystemExit(f"no fingerprint for {table}.{col}")
    fp = ColumnFingerprint.from_dict(raw)
    matches = infer_column_governance(fp, k=args.k)
    if args.json:
        print(json.dumps([m.to_dict() for m in matches], indent=2))
    else:
        for m in matches:
            g = m.golden
            print(f"{m.score:.2%}  {g.table_name}.{g.column_name}  {g.entity_type or '-'}  {g.classification or '-'}")
    return 0
