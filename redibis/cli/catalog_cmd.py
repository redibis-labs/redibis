"""CLI handlers for ``redibis catalog`` (neutral data-governance push)."""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

from redibis.config import RedibisConfig
from redibis.services.catalog_service import CatalogService
from redibis.services.catalog.openmetadata import build_openmetadata_plan, openmetadata_ui_url
from redibis.store.contract_store import ContractStore


def _load_config(args) -> RedibisConfig:
    path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
    if path:
        return RedibisConfig.from_yaml(path)
    return RedibisConfig.default()


def _catalog_service(args, store: ContractStore) -> CatalogService:
    return CatalogService.from_redibis_config(store, _load_config(args))


def run_catalog(args, store: ContractStore) -> int:
    action = args.catalog_action
    svc = _catalog_service(args, store)

    if action == "backends":
        backends = svc.list_backends()
        cfg_path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
        if getattr(args, "json", False):
            print(json.dumps({
                "backends": backends,
                "default": svc.config.backend,
                "config": cfg_path,
            }, indent=2))
            return 0
        if cfg_path:
            print(f"Config: {cfg_path}")
        else:
            print("Config: (built-in defaults — set REDIBIS_CONFIG or --config)")
        print(f"Active backend: {svc.config.backend}")
        print("Available catalog backends:")
        for name in backends:
            mark = " (active)" if name == svc.config.backend else ""
            print(f"  {name}{mark}")
        return 0

    if action == "push":
        return _catalog_push(args, svc)
    if action == "push-file":
        return _catalog_push_file(args, svc)
    if action == "push-batch":
        return _catalog_push_batch(args, svc)
    if action == "push-scan":
        return _catalog_push_scan(args, svc)
    if action == "status":
        return _catalog_status(args, svc)
    if action == "open":
        return _catalog_open(args, svc)
    if action == "feedback":
        return _catalog_feedback(args, svc)
    if action == "sync-feedback":
        print(
            "removed: 'sync-feedback' — use: catalog feedback sync",
            file=sys.stderr,
        )
        return 1
    if action == "ledger":
        return _catalog_ledger(args, svc)
    if action == "suppression":
        return _catalog_suppression(args, svc)
    if action == "suppressions":
        print(
            "removed: 'suppressions' — use: catalog suppression list|add|remove",
            file=sys.stderr,
        )
        return 1
    if action == "delete":
        return _catalog_delete(args, svc)
    if action == "wipe":
        return _catalog_wipe(args, svc)
    return 2


def _require_yes(args, *, label: str) -> bool:
    """Return True when deletion is allowed; otherwise print dry-run guidance."""
    if getattr(args, "yes", False):
        return True
    print(
        f"{label}: dry-run only. Re-run with --yes to execute irreversible deletes.",
        file=sys.stderr,
    )
    return False


def _catalog_delete(args, svc: CatalogService) -> int:
    tables = list(getattr(args, "tables", None) or [])
    if getattr(args, "table", None):
        tables.insert(0, args.table)
    fqns = list(getattr(args, "fqn", None) or [])
    if not tables and not fqns:
        print("Provide TABLE ... and/or --fqn FQN ...", file=sys.stderr)
        return 1

    dry_run = not _require_yes(args, label="catalog delete")
    hard_delete = not bool(getattr(args, "soft", False))
    try:
        result = svc.delete_tables(
            tables=tables or None,
            fqns=fqns or None,
            dry_run=dry_run,
            hard_delete=hard_delete,
            backend=getattr(args, "backend", None),
        )
    except Exception as exc:
        print(f"catalog delete failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False) or dry_run:
        print(json.dumps(result, indent=2, default=str))
        return 0

    for row in result.get("results") or []:
        state = "missing" if row.get("missing") else "deleted"
        print(f"{state} table {row.get('entity_fqn')}")
    print(
        f"catalog delete: deleted={result.get('deleted', 0)} "
        f"missing={result.get('missing', 0)}"
    )
    return 0


def _catalog_wipe(args, svc: CatalogService) -> int:
    dry_run = not _require_yes(args, label="catalog wipe")
    hard_delete = not bool(getattr(args, "soft", False))
    try:
        result = svc.wipe_catalog(
            dry_run=dry_run,
            hard_delete=hard_delete,
            with_glossary=bool(getattr(args, "with_glossary", False)),
            with_classifications=bool(getattr(args, "with_classifications", False)),
            service_name=getattr(args, "service", None),
            backend=getattr(args, "backend", None),
        )
    except Exception as exc:
        print(f"catalog wipe failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False) or dry_run:
        print(json.dumps(result, indent=2, default=str))
        return 0

    for row in result.get("results") or []:
        state = "missing" if row.get("missing") else "deleted"
        print(f"{state} {row.get('kind')} {row.get('entity_fqn')}")
    print(
        f"catalog wipe: service={result.get('service_name')} "
        f"deleted={result.get('deleted', 0)} missing={result.get('missing', 0)}"
    )
    return 0


def _enforce_kwargs(args) -> dict:
    enforce = bool(getattr(args, "enforce", False))
    clear = bool(getattr(args, "clear_suppressions", False))
    reason = (getattr(args, "reason", None) or "").strip()
    confirm = bool(getattr(args, "confirm", False))

    if clear and not enforce:
        print("--clear-suppressions requires --enforce", file=sys.stderr)
        return {"__error__": 1}

    if enforce:
        if not reason:
            print("--enforce requires --reason", file=sys.stderr)
            return {"__error__": 1}
        tables = (getattr(args, "tables", None) or "").strip()
        if not tables:
            print("--enforce requires --tables", file=sys.stderr)
            return {"__error__": 1}
        # Enforce defaults to dry-run; --confirm is required to write.
        dry_run = True if not confirm else bool(getattr(args, "dry_run", False))
        return {
            "reconcile_mode": "enforce",
            "clear_suppressions": clear,
            "enforce_reason": reason,
            "enforce_actor": os.environ.get("USER") or os.environ.get("USERNAME") or "cli",
            "dry_run": dry_run,
        }
    return {
        "reconcile_mode": "normal",
        "clear_suppressions": False,
        "enforce_reason": "",
        "enforce_actor": "",
        "dry_run": bool(getattr(args, "dry_run", False)),
    }


def _catalog_push(args, svc: CatalogService) -> int:
    backend = getattr(args, "backend", None)
    ek = _enforce_kwargs(args)
    if ek.get("__error__"):
        return int(ek["__error__"])
    fail_fast = getattr(args, "fail_fast", False)

    dry_run = ek["dry_run"]
    try:
        if getattr(args, "all", False):
            if ek["reconcile_mode"] == "enforce" and not getattr(args, "tables", None):
                print("--enforce with --all requires --tables", file=sys.stderr)
                return 1
            results = []
            errors = []
            for table in svc.store.list_tables():
                tables_glob = getattr(args, "tables", None)
                if tables_glob and not _tables_match(table, tables_glob):
                    continue
                try:
                    results.append(svc.push(
                        table,
                        dry_run=dry_run,
                        backend=backend,
                        reconcile_mode=ek["reconcile_mode"],
                        clear_suppressions=ek["clear_suppressions"],
                        enforce_reason=ek["enforce_reason"],
                        enforce_actor=ek["enforce_actor"],
                        refresh_entity=bool(getattr(args, "refresh_resolution", False)),
                    ))
                except Exception as exc:  # noqa: BLE001 — per-table isolation
                    errors.append((table, str(exc)))
                    if fail_fast:
                        break
        else:
            table = args.table
            if not table:
                print("Provide TABLE or --all", file=sys.stderr)
                return 1
            tables_glob = getattr(args, "tables", None)
            if ek["reconcile_mode"] == "enforce" and tables_glob and not _tables_match(table, tables_glob):
                print(f"TABLE {table!r} does not match --tables {tables_glob!r}", file=sys.stderr)
                return 1
            results = [svc.push(
                table,
                dry_run=dry_run,
                backend=backend,
                reconcile_mode=ek["reconcile_mode"],
                clear_suppressions=ek["clear_suppressions"],
                enforce_reason=ek["enforce_reason"],
                enforce_actor=ek["enforce_actor"],
                refresh_entity=bool(getattr(args, "refresh_resolution", False)),
            )]
            errors = []
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"catalog push failed: {exc}", file=sys.stderr)
        return 1

    # Prefer human reconcile diff when present and not JSON
    if not getattr(args, "json", False):
        for r in results:
            diff = ((r.preview or {}).get("reconcile") or {}).get("diff")
            if diff:
                print(diff)
                print()
    rc = _print_push_results(results, args, dry_run_override=dry_run)
    for table, msg in errors:
        print(f"FAILED {table}: {msg}", file=sys.stderr)
    if errors:
        print(f"{len(errors)} table(s) failed, {len(results)} succeeded", file=sys.stderr)
        return 1
    return rc


def _tables_match(table: str, pattern: str) -> bool:
    pattern = pattern.strip()
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return table == prefix or table.startswith(prefix + ".")
    if "*" in pattern:
        import fnmatch
        return fnmatch.fnmatch(table, pattern)
    return table == pattern


def _catalog_push_file(args, svc: CatalogService) -> int:
    path = getattr(args, "file", None) or getattr(args, "path", None)
    if not path:
        print("Provide a contract file path", file=sys.stderr)
        return 1
    try:
        result = svc.push_file(
            path,
            dry_run=getattr(args, "dry_run", False),
            backend=getattr(args, "backend", None),
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"catalog push-file failed: {exc}", file=sys.stderr)
        return 1

    return _print_push_results([result], args)


def _catalog_push_batch(args, svc: CatalogService) -> int:
    path = getattr(args, "path", None) or getattr(args, "dir", None)
    if not path:
        print("Provide a directory or file path", file=sys.stderr)
        return 1
    try:
        results, errors = svc.push_batch(
            path,
            glob=getattr(args, "glob", None),
            recursive=getattr(args, "recursive", False),
            dry_run=getattr(args, "dry_run", False),
            backend=getattr(args, "backend", None),
            fail_fast=getattr(args, "fail_fast", False),
        )
    except (ValueError, FileNotFoundError, NotADirectoryError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"catalog push-batch failed: {exc}", file=sys.stderr)
        return 1

    rc = _print_push_results(results, args)
    for fpath, msg in errors:
        print(f"FAILED {fpath}: {msg}", file=sys.stderr)
    if errors:
        print(f"{len(errors)} file(s) failed, {len(results)} succeeded", file=sys.stderr)
        return 1
    return rc


def _catalog_push_scan(args, svc: CatalogService) -> int:
    scan_dir = getattr(args, "scan_dir", None)
    if not scan_dir:
        print("Provide SCAN_DIR", file=sys.stderr)
        return 1
    try:
        report = svc.push_scan(
            scan_dir,
            database=getattr(args, "database", "") or "",
            include=getattr(args, "include", None) or None,
            exclude=getattr(args, "exclude", None) or None,
            tables_file=getattr(args, "tables_file", None),
            dry_run=getattr(args, "dry_run", False),
            backend=getattr(args, "backend", None),
            fail_fast=getattr(args, "fail_fast", False),
            skip_in_sync=getattr(args, "skip_in_sync", False),
            resume=getattr(args, "resume", None),
            retry_failed=not getattr(args, "no_retry_failed", False),
        )
    except (ValueError, FileNotFoundError, NotADirectoryError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"catalog push-scan failed: {exc}", file=sys.stderr)
        return 1

    results = report.get("results") or []
    errors = report.get("errors") or []
    summary = report.get("summary") or {}

    if getattr(args, "json", False) or getattr(args, "dry_run", False):
        payload = {
            "run_id": report.get("run_id"),
            "manifest": report.get("manifest"),
            "run_dir": report.get("run_dir"),
            "summary": summary,
            "results": [_result_dict(r) for r in results],
            "errors": [{"table": t, "error": e} for t, e in errors],
            "run": report.get("run"),
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"catalog push-scan run_id={report.get('run_id')}")
        print(f"  manifest: {report.get('manifest')}")
        _print_push_results(results, args)
        for table, msg in errors:
            print(f"FAILED {table}: {msg}", file=sys.stderr)
        print(
            "summary: "
            f"succeeded={summary.get('succeeded', 0)} "
            f"failed={summary.get('failed', 0)} "
            f"excluded={summary.get('excluded', 0)} "
            f"pending={summary.get('pending', 0)}"
        )

    if errors or (summary.get("failed") or 0) > 0:
        return 1
    return 0


def _print_push_results(results: list, args, *, dry_run_override: Optional[bool] = None) -> int:
    dry_run = dry_run_override if dry_run_override is not None else getattr(args, "dry_run", False)
    if getattr(args, "json", False) or dry_run:
        # push-scan handles its own JSON envelope
        if getattr(args, "catalog_action", "") == "push-scan":
            return 0
        payload = [_result_dict(r) for r in results]
        print(json.dumps(payload if len(payload) > 1 else (payload[0] if payload else {}),
                         indent=2, default=str))
        return 0

    cfg = _load_config(args)
    for r in results:
        mode = "dry-run" if r.dry_run else "pushed"
        print(f"{mode} {r.table} → {r.backend} ({r.entity_fqn})")
        if not r.dry_run and r.backend == "openmetadata":
            print(f"  open: {openmetadata_ui_url(cfg.catalog.openmetadata.host, r.entity_fqn)}")
    return 0


def _catalog_status(args, svc: CatalogService) -> int:
    table = args.table
    if not table:
        print("Provide TABLE", file=sys.stderr)
        return 1
    try:
        st = svc.status(table, backend=getattr(args, "backend", None))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(_status_dict(st), indent=2))
        return 0

    print(f"{st.table}  backend={st.backend}  contract_version={st.contract_version}")
    if st.last_push:
        print(f"  last push: {st.last_push.get('pushed_at')}  fqn={st.last_push.get('entity_fqn')}")
        if st.in_sync is not None:
            print(f"  in_sync: {st.in_sync}")
        fqn = st.last_push.get("entity_fqn")
        if fqn and st.backend == "openmetadata":
            cfg = _load_config(args)
            print(f"  open: {openmetadata_ui_url(cfg.catalog.openmetadata.host, fqn)}")
    else:
        print("  never pushed to this catalog backend")
    return 0


def _catalog_open(args, svc: CatalogService) -> int:
    table = args.table
    if not table:
        print("Provide TABLE", file=sys.stderr)
        return 1
    backend = (getattr(args, "backend", None) or svc.config.backend or "openmetadata").lower()
    if backend != "openmetadata":
        print(f"catalog open is only supported for openmetadata (got {backend!r})", file=sys.stderr)
        return 1
    try:
        contract = svc.store.get_active(table)
        if contract is None:
            raise ValueError(f"No active contract for {table!r}")
        om = svc.config.openmetadata
        plan = build_openmetadata_plan(
            contract, table,
            service_name=om.service_name,
            default_schema=om.default_schema,
        )
        url = openmetadata_ui_url(om.host, plan.table_fqn)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({"table": table, "entity_fqn": plan.table_fqn, "url": url}, indent=2))
        return 0
    print(url)
    return 0


def _catalog_feedback(args, svc: CatalogService) -> int:
    sub = getattr(args, "feedback_action", None) or "sync"
    if sub != "sync":
        print(f"unknown feedback action: {sub}", file=sys.stderr)
        return 2
    try:
        result = svc.sync_feedback(backend=getattr(args, "backend", None))
    except Exception as exc:
        print(f"catalog feedback sync failed: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(
        f"feedback sync: events={result.get('events', 0)} "
        f"suppressions={result.get('suppressions_written', 0)} "
        f"ledger_dropped={result.get('ledger_dropped', 0)} "
        f"last_seen_ms={result.get('last_seen_ms')}"
    )
    return 0


def _catalog_ledger(args, svc: CatalogService) -> int:
    # Nested `ledger show` or shorthand `ledger TABLE`
    sub = getattr(args, "ledger_action", None)
    table = getattr(args, "table", None)
    tables_glob = (getattr(args, "tables", None) or "").strip() or None
    if sub == "show":
        table = table or getattr(args, "table", None)

    if tables_glob and not table:
        tables = [
            t for t in svc.store.list_tables()
            if _tables_match(t, tables_glob)
        ]
        if not tables:
            print(f"(no tables match --tables {tables_glob!r})")
            return 0
        all_rows = []
        for t in tables:
            entries = svc.ledger_entries(t, backend=getattr(args, "backend", None))
            for e in entries:
                all_rows.append(_ledger_row(e, table=t))
        if getattr(args, "json", False):
            print(json.dumps({"tables": tables_glob, "entries": all_rows}, indent=2))
            return 0
        if not all_rows:
            print(f"(no ledger entries matching --tables {tables_glob!r})")
            return 0
        for r in all_rows:
            print(
                f"{r['table']:24} {r['facet']:20} {r['column_path'] or '-':20} "
                f"{r['value_key'] or '-':40} state={r['state']} "
                f"streak={r['negative_streak']}"
            )
        return 0

    if not table:
        print("Provide TABLE or --tables GLOB", file=sys.stderr)
        return 1
    if tables_glob and not _tables_match(table, tables_glob):
        print(f"TABLE {table!r} does not match --tables {tables_glob!r}", file=sys.stderr)
        return 1
    entries = svc.ledger_entries(table, backend=getattr(args, "backend", None))
    rows = [_ledger_row(e, table=table) for e in entries]
    if getattr(args, "json", False):
        print(json.dumps({"table": table, "entries": rows}, indent=2))
        return 0
    if not rows:
        print(f"(no ledger entries for {table})")
        return 0
    for r in rows:
        print(
            f"{r['facet']:20} {r['column_path'] or '-':20} "
            f"{r['value_key'] or '-':40} state={r['state']} "
            f"streak={r['negative_streak']}"
        )
    return 0


def _ledger_row(e, *, table: str) -> dict:
    return {
        "table": table,
        "facet": e.key.facet.value if hasattr(e.key.facet, "value") else str(e.key.facet),
        "column_path": e.key.column_path,
        "value_key": e.key.value_key,
        "asset_fqn": e.key.asset_fqn,
        "state": e.state,
        "negative_streak": e.negative_streak,
        "scan_id": e.scan_id,
        "published_at": e.published_at.isoformat() if hasattr(e.published_at, "isoformat") else str(e.published_at),
        "value_hash": e.value_hash[:12],
    }


def _catalog_suppression(args, svc: CatalogService) -> int:
    sub = getattr(args, "suppression_action", None) or "list"
    table = getattr(args, "table", None)
    if not table:
        print("Provide TABLE", file=sys.stderr)
        return 1
    backend = getattr(args, "backend", None)

    if sub == "list":
        items = svc.list_suppressions(table, backend=backend)
        rows = []
        for s in items:
            rows.append({
                "facet": s.key.facet.value if hasattr(s.key.facet, "value") else str(s.key.facet),
                "column_path": s.key.column_path,
                "value_key": s.key.value_key,
                "asset_fqn": s.key.asset_fqn,
                "actor": s.actor,
                "reason": s.reason,
                "source": s.source,
                "created_at": s.created_at.isoformat() if hasattr(s.created_at, "isoformat") else str(s.created_at),
            })
        if getattr(args, "json", False):
            print(json.dumps({"table": table, "suppressions": rows}, indent=2))
        else:
            if not rows:
                print(f"(no suppressions for {table})")
                return 0
            for r in rows:
                print(
                    f"{r['facet']:20} {r['column_path'] or '-':20} "
                    f"{r['value_key'] or '-':40} by={r['actor']} ({r['reason']})"
                )
        return 0

    if sub == "add":
        try:
            svc.add_suppression(
                table,
                column_path=getattr(args, "column", "") or "",
                facet=args.facet,
                value_key=args.value_key,
                actor=getattr(args, "actor", None) or os.environ.get("USER") or "cli",
                reason=getattr(args, "reason", None) or "manual",
                asset_fqn=getattr(args, "asset_fqn", "") or "",
                backend=backend,
            )
        except Exception as exc:
            print(f"suppression add failed: {exc}", file=sys.stderr)
            return 1
        print("suppression added")
        return 0

    if sub == "remove":
        try:
            ok = svc.remove_suppression(
                table,
                column_path=getattr(args, "column", "") or "",
                facet=args.facet,
                value_key=args.value_key,
                asset_fqn=getattr(args, "asset_fqn", "") or "",
                backend=backend,
            )
        except Exception as exc:
            print(f"suppression remove failed: {exc}", file=sys.stderr)
            return 1
        print("removed" if ok else "not found")
        return 0 if ok else 1

    print(f"unknown suppression action: {sub}", file=sys.stderr)
    return 2


def _result_dict(r) -> dict:
    out = {
        "table": r.table,
        "backend": r.backend,
        "entity_fqn": r.entity_fqn,
        "dry_run": r.dry_run,
    }
    if r.entity_id:
        out["entity_id"] = r.entity_id
    if r.contract_id:
        out["contract_id"] = r.contract_id
    if r.glossary_count:
        out["glossary_count"] = r.glossary_count
    if r.preview:
        out["preview"] = r.preview
    if r.details:
        out["details"] = r.details
    return out


def _status_dict(st) -> dict:
    return {
        "table": st.table,
        "backend": st.backend,
        "contract_version": st.contract_version,
        "last_push": st.last_push,
        "in_sync": st.in_sync,
    }
