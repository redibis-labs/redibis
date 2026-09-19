"""Batch tabular scan into a workspace. Resumable, failure-isolated."""

from __future__ import annotations

import csv
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

from redibis.store.contract_store import ContractStore
from redibis.workspace.backends import backend_for
from redibis.workspace.batch import BatchRunner
from redibis.workspace.metadata_scope import contract_store_metadata_kwargs
from redibis.workspace.model import WorkspaceRef, _utc_now_iso, parse_s3_url, slugify
from redibis.workspace.stores import WorkspaceStores

log = logging.getLogger("redibis.workspace.scan_batch")

_DEFAULT_EXTS = {".csv", ".tsv", ".parquet", ".xlsx", ".xls"}
_TABLE_SAFE = re.compile(r"[^a-zA-Z0-9_.]+")

ScanOne = Callable[[Path, str, WorkspaceStores, dict], dict]


def _table_name(path: Path, mode: str, input_root: Path, mapping: dict[str, str]) -> str:
    rel = str(path.resolve())
    if mode == "manifest" or mapping:
        if rel in mapping:
            return mapping[rel]
        # also try as-written path
        for key, table in mapping.items():
            try:
                if Path(key).resolve() == path.resolve():
                    return table
            except (OSError, RuntimeError):
                if key == str(path):
                    return table
    stem = _TABLE_SAFE.sub("_", path.stem).strip("._") or "table"
    if mode == "folder":
        parent = _TABLE_SAFE.sub("_", path.parent.name).strip("._") or "data"
        return f"{parent}.{stem}"
    return f"data.{stem}"


def load_table_manifest(path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "path" not in reader.fieldnames or "table" not in reader.fieldnames:
            raise ValueError("manifest.csv must have columns path,table")
        for row in reader:
            src = (row.get("path") or "").strip()
            table = (row.get("table") or "").strip()
            if src and table:
                mapping[src] = table
    return mapping


def discover_scan_files(
    root: Path,
    *,
    glob_pat: str = "*.csv",
    recursive: bool = False,
    extensions: set[str] | None = None,
) -> list[Path]:
    """Sorted regular files. Symlinks and escapes are skipped (text_batch pattern)."""
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    resolved = root.resolve()
    pattern = f"**/{glob_pat}" if recursive else glob_pat
    allowed = {e.lower() for e in (extensions or _DEFAULT_EXTS)}
    out: list[Path] = []
    for path in sorted(root.glob(pattern)):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if not path.resolve().is_relative_to(resolved):
                continue
        except (OSError, RuntimeError):
            continue
        if path.suffix.lower() not in allowed and glob_pat == "*.csv":
            continue
        if any(part.startswith(".") for part in path.relative_to(resolved).parts):
            continue
        out.append(path)
    return out


def open_workspace_root(root: str, *, name: str = "") -> WorkspaceStores:
    """Bind stores to a local folder or s3:// URL without registering it.

    Trusted CLI entry point: the operator-supplied path is used as-is.
    Unlike ``WorkspaceRegistry.add_local``, this does **not** apply
    ``workspaces.allowed_roots`` (or configs-dir / default-store) containment.
    Do not reuse this from an HTTP-reachable path without those checks.
    """
    text = (root or "").strip()
    if text.startswith("s3://"):
        bucket, prefix = parse_s3_url(text)
        ref = WorkspaceRef(
            slug=slugify(name or bucket),
            name=name or bucket,
            kind="s3",
            root=text,
            created=_utc_now_iso(),
        )
        backend, bucket_name = backend_for(ref)
        contract = ContractStore(
            backend, bucket=bucket_name,
            **contract_store_metadata_kwargs(ref, backend, bucket_name),
        )
        return WorkspaceStores(ref, contract)
    path = Path(text).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    ref = WorkspaceRef(
        slug=slugify(name or resolved.name),
        name=name or resolved.name,
        kind="local",
        root=str(resolved),
        created=_utc_now_iso(),
    )
    backend, bucket_name = backend_for(ref)
    contract = ContractStore(
        backend, bucket=bucket_name,
        **contract_store_metadata_kwargs(ref, backend, bucket_name),
    )
    stores = WorkspaceStores(ref, contract)
    from redibis.workspace.atomic import atomic_put_json
    from redibis.workspace.model import WORKSPACE_LAYOUT
    if not backend.exists(bucket_name, WORKSPACE_LAYOUT["manifest"]):
        atomic_put_json(backend, bucket_name, WORKSPACE_LAYOUT["manifest"], {
            "name": ref.name, "slug": ref.slug, "kind": ref.kind,
            "root": ref.root, "created": ref.created, "layout": WORKSPACE_LAYOUT,
        })
    return stores


def _default_scan_one(path: Path, table: str, stores: WorkspaceStores, options: dict) -> dict:
    """Existing single-file scan path, writing into the workspace store."""
    from redibis.services.code_scan_session import CodeScanSession
    from redibis.services.scan_service import ScanConfig, to_scan_config
    from redibis.config import RedibisConfig

    mode = str(options.get("scan_mode") or "both")
    if mode == "both":
        mode = "all"
    cfg_path = options.get("config")
    rb = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
    rb.table = table
    if options.get("pii_engines"):
        rb.pii.engines = str(options["pii_engines"])
    if options.get("equation"):
        rb.pii.equation_mode = str(options["equation"])
    scan_config = to_scan_config(
        rb,
        table=table,
        output_dir=str(Path(stores.ref.root) / "runs" / table.replace(".", "_"))
        if stores.ref.kind == "local" else None,
    )
    session = CodeScanSession.create(
        path,
        table=table,
        output_root=Path(stores.ref.root) / "runs" / table.replace(".", "_") if stores.ref.kind == "local" else Path("./scan_output"),
        backend=stores.backend,
        store=stores.contract,
        runs_bucket=stores.bucket or "pii-reports",
        filename=path.name,
    )
    result = session.scan(
        mode=mode,
        auto_write=True,
        automerge="both",
        config=scan_config,
        redibis_config=rb,
    )
    return {
        "run_id": getattr(result, "run_id", "") or "",
        "status": getattr(result, "status", "complete"),
        "table": table,
    }


def run_scan_batch(
    *,
    input_dir: Path,
    workspace: str,
    glob_pat: str = "*.csv",
    recursive: bool = False,
    table_from: str = "filename",
    manifest: Path | None = None,
    workers: int = 1,
    resume: bool = False,
    continue_on_error: bool = False,
    limit: int | None = None,
    options: dict | None = None,
    scan_one: ScanOne | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    stores = open_workspace_root(workspace)
    mapping = load_table_manifest(manifest) if manifest else {}
    mode = "manifest" if mapping else (table_from or "filename")
    files = discover_scan_files(input_dir, glob_pat=glob_pat, recursive=recursive)
    if limit:
        files = files[: int(limit)]
    tables_and_paths = [(_table_name(p, mode, input_dir, mapping), p) for p in files]
    runner = BatchRunner(stores)
    if resume and batch_id:
        bid = runner.submit("scan", [], options=options or {}, resume=True, batch_id=batch_id)
        man = runner.status(bid)
        done = {i["table"] for i in man.get("items") or [] if i.get("status") == "done"}
        pending = [(t, p) for t, p in tables_and_paths if t not in done]
        # merge new pending items
        existing_tables = {i["table"] for i in man.get("items") or []}
        for table, _path in pending:
            if table not in existing_tables:
                man["items"].append({"table": table, "status": "pending", "error": "", "run_id": "", "path": str(_path)})
        runner._put(man)
    else:
        bid = runner.submit("scan", [t for t, _ in tables_and_paths], options=options or {}, batch_id=batch_id)
        man = runner.status(bid)
        by_table = {t: str(p) for t, p in tables_and_paths}
        for item in man["items"]:
            item["path"] = by_table.get(item["table"], "")
        runner._put(man)

    scan_fn = scan_one or _default_scan_one
    path_by_table = {t: p for t, p in tables_and_paths}

    def _one(table: str, opts: dict) -> dict:
        path = path_by_table.get(table)
        if path is None:
            # resume: path may be on the item
            man_now = runner.status(bid)
            for item in man_now.get("items") or []:
                if item.get("table") == table and item.get("path"):
                    path = Path(item["path"])
                    break
        if path is None:
            raise FileNotFoundError(f"no input path for table {table}")
        return scan_fn(Path(path), table, stores, opts or {})

    workers = max(1, int(workers or 1))
    if workers == 1:
        man = runner.run(bid, item_fn=_one)
    else:
        man = runner.status(bid)
        man["status"] = "running"
        runner._put(man)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for item in man["items"]:
                if item.get("status") == "done":
                    continue
                futures[pool.submit(_one, item["table"], options or {})] = item
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    result = fut.result()
                    item["status"] = "done"
                    item["run_id"] = str((result or {}).get("run_id") or "")
                    item["error"] = ""
                except Exception as exc:
                    item["status"] = "error"
                    item["error"] = str(exc)
                    man.setdefault("errors", []).append({"table": item["table"], "error": str(exc)})
                runner._recount(man)
                runner._put(man)
        man["finished"] = _utc_now_iso()
        man["status"] = "done"
        runner._recount(man)
        runner._put(man)

    try:
        stores.index.rebuild()
    except Exception:
        log.warning("scan-batch reindex failed", exc_info=True)

    errors = int((man.get("counts") or {}).get("error") or 0)
    man["exit_nonzero"] = bool(errors) and not continue_on_error
    return man
