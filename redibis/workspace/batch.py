"""Resumable, failure-isolated batch runner for a workspace."""

from __future__ import annotations

import logging
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Callable, Optional

from redibis.workspace.atomic import atomic_put_json
from redibis.workspace.index import WorkspaceIndex
from redibis.workspace.model import WORKSPACE_LAYOUT, WorkspaceDenied, WorkspaceRef
from redibis.workspace.stores import WorkspaceStores, stores_for

log = logging.getLogger("redibis.workspace.batch")

KINDS = frozenset({
    "scan", "enrich", "synthesize", "export", "reindex", "steward_finalize", "promote",
})
_BATCH_PREFIX = WORKSPACE_LAYOUT["batches"].rstrip("/")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batch_key(batch_id: str) -> str:
    return f"{_BATCH_PREFIX}/{batch_id}.json"


class BatchRunner:
    def __init__(self, stores: WorkspaceStores):
        self.stores = stores
        self._lock = threading.RLock()

    def _get(self, batch_id: str) -> dict:
        key = _batch_key(batch_id)
        if not self.stores.backend.exists(self.stores.bucket, key):
            raise KeyError(batch_id)
        return self.stores.backend.get_json(self.stores.bucket, key) or {}

    def _put(self, manifest: dict) -> None:
        atomic_put_json(
            self.stores.backend, self.stores.bucket,
            _batch_key(manifest["id"]), manifest,
        )

    def list_recent(self, limit: int = 20) -> list[dict]:
        keys = self.stores.backend.list_keys(self.stores.bucket, prefix=f"{_BATCH_PREFIX}/")
        rows = []
        for key in sorted(keys, reverse=True)[: max(limit * 2, 20)]:
            if not key.endswith(".json"):
                continue
            try:
                rows.append(self.stores.backend.get_json(self.stores.bucket, key))
            except Exception:
                continue
        rows.sort(key=lambda m: str((m or {}).get("started") or ""), reverse=True)
        return rows[:limit]

    def status(self, batch_id: str) -> dict:
        return self._get(batch_id)

    def cancel(self, batch_id: str) -> dict:
        with self._lock:
            man = self._get(batch_id)
            if man.get("status") in ("done", "cancelled"):
                return man
            man["cancel_requested"] = True
            man["status"] = "cancelling"
            self._put(man)
            return man

    @staticmethod
    def _recount(man: dict) -> None:
        counts = {"total": len(man.get("items") or []), "done": 0, "error": 0, "skipped": 0}
        for item in man.get("items") or []:
            st = item.get("status")
            if st == "done":
                counts["done"] += 1
            elif st == "error":
                counts["error"] += 1
            elif st in ("skipped", "cancelled"):
                counts["skipped"] += 1
        man["counts"] = counts

    def submit(
        self,
        kind: str,
        tables: list[str],
        options: dict | None = None,
        *,
        actor: str = "",
        batch_id: str | None = None,
        resume: bool = False,
    ) -> str:
        kind = (kind or "").strip()
        if kind not in KINDS:
            raise ValueError(f"unknown batch kind {kind!r}")
        if self.stores.ref.read_only and kind not in ("export", "reindex"):
            raise WorkspaceDenied("workspace is read-only")
        options = dict(options or {})
        if resume and batch_id:
            man = self._get(batch_id)
            man["status"] = "running"
            man["cancel_requested"] = False
            man["resume"] = True
            for item in man.get("items") or []:
                if item.get("status") in ("error", "running"):
                    item["status"] = "pending"
                    item["error"] = ""
            self._recount(man)
            self._put(man)
            return batch_id
        bid = batch_id or uuid.uuid4().hex[:12]
        items = [
            {"table": t, "status": "pending", "error": "", "run_id": ""}
            for t in tables
        ]
        man = {
            "id": bid,
            "kind": kind,
            "workspace": self.stores.ref.slug,
            "items": items,
            "options": options,
            "actor": actor or "",
            "counts": {"total": len(items), "done": 0, "error": 0, "skipped": 0},
            "started": _utc_now_iso(),
            "finished": "",
            "status": "pending",
            "cancel_requested": False,
            "errors": [],
        }
        self._put(man)
        return bid

    def run(self, batch_id: str, *, item_fn: Callable[[str, dict], dict] | None = None) -> dict:
        with self._lock:
            man = self._get(batch_id)
            man["status"] = "running"
            self._put(man)
        kind = man["kind"]
        options = man.get("options") or {}
        fn = item_fn or (lambda table, opts: self._dispatch(kind, table, opts))
        for item in man["items"]:
            latest = self._get(batch_id)
            if latest.get("cancel_requested"):
                man["cancel_requested"] = True
                if item.get("status") not in ("done", "error"):
                    item["status"] = "skipped"
                self._recount(man)
                self._put(man)
                continue
            if item.get("status") == "done":
                continue
            item["status"] = "running"
            self._put(man)
            try:
                result = fn(item["table"], options)
                item["status"] = "done"
                item["run_id"] = str((result or {}).get("run_id") or "")
                item["result"] = result or {}
                item["error"] = ""
                try:
                    self.stores.touch(item["table"])
                except Exception:
                    pass
            except Exception as exc:
                log.exception("batch %s item %s failed", batch_id, item.get("table"))
                item["status"] = "error"
                item["error"] = str(exc)
                man.setdefault("errors", []).append({"table": item["table"], "error": str(exc)})
            self._recount(man)
            self._put(man)
        man["finished"] = _utc_now_iso()
        man["status"] = "cancelled" if man.get("cancel_requested") else "done"
        self._recount(man)
        self._put(man)
        return man

    def _dispatch(self, kind: str, table: str, options: dict) -> dict:
        stores = self.stores
        if kind == "reindex":
            n = stores.index.rebuild()
            return {"rebuilt": n}
        if kind == "enrich":
            return _run_enrich(stores, table, options)
        if kind == "synthesize":
            return _run_synthesize(stores, table, options)
        if kind == "steward_finalize":
            return _run_finalize(stores, table, options)
        if kind == "promote":
            from redibis.workspace.promote import promote_table
            target = str(options.get("target") or "default")
            force = bool(options.get("force"))
            return promote_table(
                stores, table, target=target, force=force,
                actor=str(options.get("actor") or ""),
            )
        if kind == "export":
            return {"table": table}
        raise ValueError(f"unsupported kind {kind!r}")


def _run_enrich(stores: WorkspaceStores, table: str, options: dict) -> dict:
    from redibis.enrich.providers import get_provider

    provider_name = str(options.get("provider") or "demo")
    provider = get_provider(provider_name)
    result = stores.enrich.enrich(
        table, provider,
        extra_instructions=str(options.get("extra_instructions") or "") or None,
        enriched_by=str(options.get("actor") or "batch"),
        write_active=True,
    )
    return result.to_dict() if hasattr(result, "to_dict") else {"table": table}


def _run_synthesize(stores: WorkspaceStores, table: str, options: dict) -> dict:
    from redibis.synthesis import ContractSynthesisRunner

    base = stores.contract.get_active(table)
    if not base:
        raise ValueError(f"no active contract for {table}")
    runner = ContractSynthesisRunner(
        analysis_mode=str(options.get("analysis_mode") or "deterministic"),
    )
    result = runner.run(base_contract=base, output_dir=None)
    candidate = getattr(result, "candidate", None) or {}
    if candidate:
        stores.contract.upsert(candidate, table=table, workflow="synthesis",
                               run_id=str(getattr(result, "run_id", "") or "synth"))
    payload = result.to_dict() if hasattr(result, "to_dict") else {"table": table}
    return payload


def _run_finalize(stores: WorkspaceStores, table: str, options: dict) -> dict:
    actor = str(options.get("actor") or "batch")
    result = stores.steward.finalize(table, actor=actor)
    if not result.get("ok"):
        raise ValueError(result.get("message") or "guarantee blocked")
    return result


def export_zip(stores: WorkspaceStores, tables: list[str], artifacts: list[str]) -> bytes:
    """Zip requested artifacts for the given tables. Nothing else."""
    from redibis.review.artifacts import get_artifact
    import yaml

    wanted = [a.strip() for a in artifacts if a.strip()]
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for table in tables:
            for name in wanted:
                if name in ("contract", "contract.yaml", "active"):
                    active = stores.contract.get_active(table)
                    if active:
                        zf.writestr(
                            f"{table}/contract.yaml",
                            yaml.safe_dump(active, sort_keys=False, allow_unicode=True),
                        )
                    continue
                try:
                    body, _ct, filename = get_artifact(stores.contract, table, name)
                except Exception:
                    continue
                zf.writestr(f"{table}/{filename}", body)
    return buf.getvalue()


def runner_for(slug: str) -> BatchRunner:
    return BatchRunner(stores_for(slug))
