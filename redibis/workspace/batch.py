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
    "scan", "enrich", "synthesize", "reindex", "steward_finalize", "promote",
})
_BATCH_PREFIX = WORKSPACE_LAYOUT["batches"].rstrip("/")
_EXPORT_HINT = (
    "batch kind 'export' is not supported; use GET /api/workspaces/{slug}/export "
    "or `redibis workspace export`"
)


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
        if kind == "export":
            raise ValueError(_EXPORT_HINT)
        if kind not in KINDS:
            raise ValueError(f"unknown batch kind {kind!r}")
        if self.stores.ref.read_only and kind not in ("reindex",):
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
            raise ValueError(_EXPORT_HINT)
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
    """Deep Enrich batch: persist a separate candidate. Never upserts active."""
    from redibis.services.deep_enrich_service import DeepEnrichService
    from redibis.store.subcontract_store import SubcontractStore

    analysis_mode = str(options.get("analysis_mode") or "deterministic").lower()
    provider_obj = None
    if analysis_mode == "assisted":
        from redibis.enrich.providers import get_provider

        try:
            provider_obj = get_provider(str(options.get("provider") or ""))
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if provider_obj is None:
            raise ValueError("assisted mode requested but no provider configured")

    if stores.backend is None:
        raise ValueError("workspace store has no backend")
    subs = SubcontractStore(stores.backend)
    svc = DeepEnrichService(stores.contract, subs)
    result = svc.run(
        table,
        provider=provider_obj,
        analysis_mode=analysis_mode or "deterministic",
        system_prompt=str(options.get("system_prompt") or ""),
        extra_context=str(options.get("extra_context") or ""),
        created_by=str(options.get("actor") or "batch"),
    )
    # Also mirror portable artifacts under runs/ for workspace explorers
    run_id = result.get("run_id") or uuid.uuid4().hex[:12]
    try:
        base = stores.contract.get_active(table) or {}
        candidate = (result.get("payload") or {}).get("candidate") or {}
        artifacts = _persist_synthesis_artifacts(
            stores, table, run_id,
            type("R", (), {
                "candidate": candidate,
                "evidence": (result.get("payload") or {}).get("evidence"),
                "lineage": (result.get("payload") or {}).get("lineage"),
                "lineage_react_flow": result.get("lineage_react_flow"),
                "traceability": result.get("traceability"),
                "stage_results": (result.get("payload") or {}).get("stage_results"),
                "meta": result.get("meta"),
                "comparison": (result.get("payload") or {}).get("comparison"),
            })(),
            base,
        )
        result = dict(result)
        result["artifacts"] = {**(result.get("artifacts") or {}), **artifacts}
    except Exception:
        pass
    result["writes_active_contract"] = False
    return result


def _persist_synthesis_artifacts(
    stores: WorkspaceStores,
    table: str,
    run_id: str,
    result: Any,
    base: dict,
) -> dict[str, str]:
    """Write synthesis outputs under ``runs/{table}/synthesis/{run_id}/``."""
    import yaml

    backend = stores.backend
    if backend is None:
        raise ValueError("workspace store has no backend")
    bucket = stores.bucket
    prefix = f"runs/{table}/synthesis/{run_id}"
    written: dict[str, str] = {}

    def _yaml(name: str, data: Any) -> None:
        key = f"{prefix}/{name}"
        backend.put_text(
            bucket, key,
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            content_type="application/x-yaml",
        )
        written[name] = key

    def _json(name: str, data: Any) -> None:
        key = f"{prefix}/{name}"
        backend.put_json(bucket, key, data)
        written[name] = key

    _yaml("contract.base.v3.yaml", base)
    candidate = getattr(result, "candidate", None) or {}
    _yaml("contract.synthesized.v3.1.yaml", candidate)
    _json("contract.synthesized.v3.1.json", candidate)
    evidence = getattr(result, "evidence", None)
    if evidence:
        _json("evidence_bundle.json", evidence)
    lineage = getattr(result, "lineage", None)
    if lineage:
        _json("lineage.json", lineage)
    flow = getattr(result, "lineage_react_flow", None)
    if flow:
        _json("lineage_graph.json", flow)
    traceability = getattr(result, "traceability", None)
    if traceability:
        _json("requirements_traceability.json", traceability)
    stages = getattr(result, "stage_results", None)
    if stages:
        _json("stage_results.json", stages)
    meta = getattr(result, "meta", None)
    if meta:
        atomic_put_json(backend, bucket, f"{prefix}/synthesis_meta.json", meta)
        written["synthesis_meta.json"] = f"{prefix}/synthesis_meta.json"
    comparison = getattr(result, "comparison", None)
    if comparison:
        _json("analysis_comparison.json", comparison)
    return written


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
