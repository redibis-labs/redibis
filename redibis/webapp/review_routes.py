"""
redibis.webapp.review_routes
============================
REST surface for Final-Review and Evidence Review Ledger features.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional, Union

from fastapi import HTTPException
from pydantic import BaseModel

from redibis.services.evidence_review_service import EvidenceReviewService
from redibis.services.review_service import ReviewInputError, ReviewService
from redibis.store.contract_store import ContractStore
from redibis.store.review_store import ReviewStore

StoreGetter = Union[ContractStore, Callable[[], ContractStore]]


class ColumnEditBody(BaseModel):
    tags: Optional[list] = None
    classification: Optional[str] = None
    definition: Optional[str] = None
    glossary: Optional[list] = None
    pii_status: Optional[str] = None          # "pii" | "not_pii"
    entity_type: Optional[str] = None         # entity correction, with pii_status="pii"
    reviewer: str = ""
    reason: str = ""
    run_id: Optional[str] = None              # evidence run this decision was made against


class ColumnApproveBody(BaseModel):
    approved: Optional[dict] = None           # snapshot the reviewer approved (optional)
    reviewer: str = ""
    note: str = ""


class ReviewerBody(BaseModel):
    reviewer: str = ""
    note: str = ""


class EvidenceBundleBody(BaseModel):
    bundle: dict
    mode: str = "artifact"
    session_id: str = ""


class RestrictedLlmBody(BaseModel):
    run_id: str
    call_id: str
    actor: str = ""
    reason: str = ""
    role: str = ""


class VerdictPreviewBody(BaseModel):
    package: dict
    run_id: Optional[str] = None


class VerdictImportBody(BaseModel):
    package: dict
    run_id: Optional[str] = None
    actor: str = ""
    reason: str = ""
    merge_policy: str = "matching_only"  # matching_only | overwrite_conflicts


def register_review_routes(app: Any, store_getter: StoreGetter) -> None:
    def _store() -> ContractStore:
        return store_getter() if callable(store_getter) else store_getter

    def _review_store(store: ContractStore) -> ReviewStore:
        return ReviewStore(store.backend, store.bucket)

    def _svc() -> ReviewService:
        store = _store()
        return ReviewService(store, _review_store(store))

    def _evidence_svc() -> EvidenceReviewService:
        store = _store()
        return EvidenceReviewService(
            store,
            review_store=_review_store(store),
            pii_store=store.pii_decisions,
        )

    def _svc_call(fn, *a, **k):
        try:
            return fn(*a, **k)
        except ReviewInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/contracts/{table}/review")
    def get_review(table: str) -> dict:
        return _svc_call(_svc().get_review, table)

    @app.get("/api/contracts/{table}/review/status")
    def review_status(table: str) -> dict:
        return _svc_call(_svc().status, table)

    @app.post("/api/contracts/{table}/review/columns/{column}/approve")
    def approve_column(table: str, column: str, body: ColumnApproveBody) -> dict:
        return _svc_call(_svc().approve_column, table, column,
                         approved=body.approved, reviewer=body.reviewer, note=body.note)

    @app.put("/api/contracts/{table}/review/columns/{column}")
    def edit_column(table: str, column: str, body: ColumnEditBody) -> dict:
        evidence_col_block: Optional[dict] = None
        if body.run_id and body.pii_status in ("pii", "not_pii"):
            # Best-effort: a PII/not-PII decision should fingerprint against the
            # run the steward was actually looking at when they decided (plan
            # §5). If the run's evidence is unavailable, fall back to the
            # contract-property fingerprint rather than failing the write.
            try:
                bundle, _hint = _load_bundle(table, body.run_id)
                columns = bundle.get("columns") if isinstance(bundle.get("columns"), dict) else {}
                block = columns.get(column)
                if isinstance(block, dict):
                    evidence_col_block = block
            except HTTPException:
                evidence_col_block = None
        return _svc_call(_svc().edit_column, table, column,
                         tags=body.tags, classification=body.classification,
                         definition=body.definition, glossary=body.glossary,
                         pii_status=body.pii_status, entity_type=body.entity_type,
                         reason=body.reason, reviewer=body.reviewer,
                         run_id=body.run_id or "", evidence_col_block=evidence_col_block)

    @app.post("/api/contracts/{table}/review/columns/{column}/reject")
    def reject_column(table: str, column: str, body: ReviewerBody) -> dict:
        return _svc_call(_svc().reject_column, table, column, reviewer=body.reviewer, note=body.note)

    @app.post("/api/contracts/{table}/review/columns/{column}/reset")
    def reset_column(table: str, column: str) -> dict:
        return _svc_call(_svc().reset_column, table, column)

    @app.get("/api/contracts/{table}/review/columns/{column}/history")
    def column_decision_history(table: str, column: str) -> dict:
        """Append-only PII-decision audit trail for one column (current first)."""
        store = _store()
        history = store.pii_decisions.history(table, column)
        return {"table": table, "column": column, "history": history}

    @app.post("/api/contracts/{table}/review/finalize")
    def finalize_review(table: str, body: ReviewerBody) -> dict:
        return _svc_call(_svc().finalize, table, reviewer=body.reviewer)

    @app.get("/api/contracts/{table}/verdicts/export")
    def export_verdicts(table: str) -> dict:
        return _svc_call(_evidence_svc().export_verdicts, tables=[table])

    @app.get("/api/verdicts/export")
    def export_all_verdicts() -> dict:
        return _svc_call(_evidence_svc().export_verdicts)

    @app.post("/api/evidence/review")
    def evidence_review_from_bundle(body: EvidenceBundleBody) -> dict:
        return _svc_call(
            _evidence_svc().from_bundle,
            body.bundle,
            mode=body.mode,
            session_id=body.session_id,
        )

    def _evidence_output_dirs() -> list[Path]:
        import os
        from redibis.config import RedibisConfig

        cfg_path = os.environ.get("REDIBIS_CONFIG")
        cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
        output_dirs: list[Path] = []
        seen_dirs: set[str] = set()
        for raw in (
            os.environ.get("SCAN_OUTPUT_DIR"),
            "./scan_output",
            str(cfg.report.output_dir),
            "./reports",
        ):
            if not raw:
                continue
            path = Path(raw)
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            if path.exists():
                output_dirs.append(path)
        return output_dirs

    def _load_bundle(table: str, run_id: Optional[str]) -> tuple[dict, str]:
        from redibis.scan.evidence_ops import EvidenceError, load_evidence_bundle
        from redibis.webapp.store_accessors import get_runs_bucket

        store = _store()
        try:
            return load_evidence_bundle(
                table=table,
                run_id=run_id,
                latest=not run_id,
                output_dirs=_evidence_output_dirs() or None,
                backend=store.backend,
                bucket=get_runs_bucket(),
            )
        except EvidenceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _load_manifest(table: str, run_id: str, hint: str) -> Optional[dict]:
        from redibis.scan.evidence_ops import load_evidence_manifest
        from redibis.webapp.store_accessors import get_runs_bucket

        store = _store()
        try:
            return load_evidence_manifest(
                table=table, run_id=run_id, hint=hint,
                output_dirs=_evidence_output_dirs() or None,
                backend=store.backend, bucket=get_runs_bucket(),
            )
        except Exception:
            return None

    @app.get("/api/evidence/{table}/review")
    def evidence_review_for_table(
        table: str,
        run_id: Optional[str] = None,
    ) -> dict:
        # Side-effect free: drift is computed here for display only (via
        # from_bundle's per-column ``drift`` block) and persisted at contract
        # write time (ContractStore.upsert -> evaluate_and_mark_drift) or via
        # the explicit POST .../recompute-drift action below — never on a GET.
        bundle, hint = _load_bundle(table, run_id)
        resolved_run_id = str((bundle.get("table") or {}).get("run_id") or run_id or "")
        manifest = _load_manifest(table, resolved_run_id, hint)
        svc = _evidence_svc()
        return svc.from_bundle(bundle, table=table, mode="run", manifest=manifest)

    @app.post("/api/evidence/{table}/review/recompute-drift")
    def evidence_recompute_drift(table: str, run_id: Optional[str] = None) -> dict:
        """Explicit steward action: evaluate + persist drift for *table* now.

        Unlike the GET review endpoints, this intentionally writes to the PII
        decision overlay (demotes fingerprint-mismatched active decisions to
        ``stale``) — see plan §5: drift is evaluated at scan completion or an
        explicit review action, never merely by opening the reviewer.
        """
        bundle, _hint = _load_bundle(table, run_id)
        svc = _evidence_svc()
        stale = svc.mark_stale_on_drift(table, bundle)
        return {"table": table, "run_id": run_id or "", "stale_columns_marked": stale}

    @app.get("/api/evidence/{table}/runs")
    def evidence_list_runs(table: str) -> dict:
        """Every run id that has evidence for *table*, newest last."""
        from redibis.scan.evidence_ops import list_evidence_runs
        from redibis.webapp.store_accessors import get_runs_bucket

        store = _store()
        output_dirs = _evidence_output_dirs()
        # list_evidence_runs takes one output_dir; merge across all configured roots.
        run_ids: list[str] = []
        seen: set[str] = set()
        for d in (output_dirs or [None]):
            for rid in list_evidence_runs(
                table=table, output_dir=d, backend=store.backend, bucket=get_runs_bucket(),
            ):
                if rid not in seen:
                    seen.add(rid)
                    run_ids.append(rid)
        return {"table": table, "runs": run_ids}

    @app.get("/api/evidence/{table}/runs/compare")
    def evidence_compare_runs(table: str, a: str, b: str) -> dict:
        """Phase/engine/proposal/steward-effective-result diff between two runs."""
        review_a = evidence_review_for_table(table, run_id=a)
        review_b = evidence_review_for_table(table, run_id=b)
        cols_a = {c["column"]: c for c in review_a.get("columns") or []}
        cols_b = {c["column"]: c for c in review_b.get("columns") or []}
        names = sorted(set(cols_a) | set(cols_b))
        columns: list[dict] = []
        for name in names:
            ca, cb = cols_a.get(name), cols_b.get(name)
            ea = (ca or {}).get("effective_verdict") or {}
            eb = (cb or {}).get("effective_verdict") or {}
            changed = bool(ca) != bool(cb) or ea.get("detected") != eb.get("detected") or (
                ea.get("entity_type") != eb.get("entity_type")
            )
            columns.append({
                "column": name,
                "present_in_a": ca is not None,
                "present_in_b": cb is not None,
                "effective_verdict_a": ea or None,
                "effective_verdict_b": eb or None,
                "engine_proposal_a": (ca or {}).get("engine_proposal"),
                "engine_proposal_b": (cb or {}).get("engine_proposal"),
                "changed": changed,
            })
        return {
            "table": table,
            "run_a": review_a.get("run_id") or a,
            "run_b": review_b.get("run_id") or b,
            "columns": columns,
            "changed_count": sum(1 for c in columns if c["changed"]),
        }

    @app.get("/api/evidence/{table}/runs/{run_id}")
    def evidence_run_overview(table: str, run_id: str) -> dict:
        return evidence_review_for_table(table, run_id=run_id)

    @app.get("/api/evidence/{table}/runs/{run_id}/columns/{column}")
    def evidence_run_column(table: str, run_id: str, column: str) -> dict:
        review = evidence_review_for_table(table, run_id=run_id)
        for item in review.get("columns") or []:
            if item.get("column") == column:
                out = dict(item)
                out["table"] = table
                out["run_id"] = review.get("run_id") or run_id
                out["header"] = review.get("header")
                return out
        raise HTTPException(status_code=404, detail=f"column {column!r} not found in run {run_id!r}")

    @app.get("/api/evidence/{table}/runs/{run_id}/artifacts")
    def evidence_run_artifacts(table: str, run_id: str) -> dict:
        bundle, hint = _load_bundle(table, run_id)
        resolved_run_id = str((bundle.get("table") or {}).get("run_id") or run_id)
        manifest = _load_manifest(table, resolved_run_id, hint)
        if not manifest:
            return {"table": table, "run_id": run_id, "artifacts": {}}
        return {
            "table": table,
            "run_id": run_id,
            "artifacts": manifest.get("artifacts") or {},
            "coverage": manifest.get("coverage") or {},
        }

    @app.get("/api/evidence/{table}/runs/{run_id}/artifacts/{name:path}")
    def evidence_run_artifact_content(table: str, run_id: str, name: str) -> Any:
        from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

        bundle, hint = _load_bundle(table, run_id)
        resolved_run_id = str((bundle.get("table") or {}).get("run_id") or run_id)
        manifest = _load_manifest(table, resolved_run_id, hint)
        if not manifest:
            raise HTTPException(status_code=404, detail="no manifest for this run")
        ref = (manifest.get("artifacts") or {}).get(name)
        if not isinstance(ref, dict):
            raise HTTPException(status_code=404, detail=f"artifact {name!r} not in manifest")
        if ref.get("sensitivity") == "restricted":
            raise HTTPException(
                status_code=403,
                detail="restricted artifact — use /api/evidence/{table}/restricted/llm with actor+reason",
            )
        local_name = ref.get("path") or ref.get("name") or name
        if hint and not str(hint).startswith("s3://"):
            candidate = Path(hint).parent / str(local_name)
            if candidate.is_file():
                if candidate.suffix in (".json",):
                    try:
                        return JSONResponse(json.loads(candidate.read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError):
                        pass
                if candidate.suffix in (".html", ".htm"):
                    return FileResponse(candidate)
                try:
                    return PlainTextResponse(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    return FileResponse(candidate)
        storage_key = ref.get("storage_key") or ""
        if storage_key:
            from redibis.webapp.store_accessors import get_runs_bucket

            store = _store()
            try:
                data = store.backend.get_json(get_runs_bucket(), storage_key)
                return JSONResponse(data)
            except Exception as exc:
                raise HTTPException(status_code=404, detail=f"artifact not retrievable: {exc}") from exc
        raise HTTPException(status_code=404, detail=f"artifact {name!r} not locally available")

    @app.post("/api/evidence/{table}/restricted/llm")
    def evidence_restricted_llm(table: str, body: RestrictedLlmBody) -> dict:
        from redibis.evidence.restricted_service import RestrictedAccessError, read_restricted_llm_call

        try:
            return read_restricted_llm_call(
                table=table, run_id=body.run_id, call_id=body.call_id,
                actor=body.actor, reason=body.reason, role=body.role,
                output_dirs=_evidence_output_dirs(),
            )
        except RestrictedAccessError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def _parse_package(raw: dict):
        from redibis.review.verdict_package import VerdictEntry, VerdictPackage, VerdictPackageError

        try:
            return VerdictPackage(entries=[
                VerdictEntry.from_dict(e) for e in (raw.get("entries") or []) if isinstance(e, dict)
            ])
        except (VerdictPackageError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid verdict package: {exc}") from exc

    @app.post("/api/evidence/{table}/verdicts/preview")
    def evidence_verdicts_preview(table: str, body: VerdictPreviewBody) -> dict:
        """Classify a verdict package's entries for *table* — matching, stale,
        missing, conflicting, or invalid — before any write (plan §6)."""
        package = _parse_package(body.package)
        bundle, _hint = _load_bundle(table, body.run_id)
        return _svc_call(_evidence_svc().preview_verdicts, table, bundle, package)

    @app.post("/api/evidence/{table}/verdicts/import")
    def evidence_verdicts_import(table: str, body: VerdictImportBody) -> dict:
        """Durably promote matching (or explicitly conflict-overriding) entries
        into the PII decision overlay. Requires an actor + reason; records an
        import audit event in ``ContractStore.verdict_import_log``."""
        package = _parse_package(body.package)
        bundle, _hint = _load_bundle(table, body.run_id)
        return _svc_call(
            _evidence_svc().import_verdicts, table, bundle, package,
            actor=body.actor, reason=body.reason, merge_policy=body.merge_policy,
        )

    @app.get("/api/evidence/{table}/verdicts/imports")
    def evidence_verdicts_import_log(table: str) -> dict:
        store = _store()
        return {"table": table, "imports": store.verdict_import_log.list(table)}

    @app.post("/api/evidence/{table}/runs/{run_id}/verdicts/replay")
    def evidence_run_replay_verdicts(table: str, run_id: str, body: VerdictPreviewBody) -> dict:
        """Run-scoped, non-mutating verdict replay — the API equivalent of
        ``redibis scan decide --verdicts``. Never writes; the returned review
        payload and warnings show what *would* apply if promoted."""
        from redibis.review.verdict_package import VerdictPackageError

        package = _parse_package(body.package)
        bundle, _hint = _load_bundle(table, run_id)
        svc = _evidence_svc()
        pii_decisions = {}
        try:
            pii_decisions = _store().get_pii_decisions(table)
        except Exception:
            pii_decisions = {}
        try:
            review = svc.from_bundle(bundle, table=table, mode="run_replay", supplied_verdicts=package)
        except VerdictPackageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        applied = [
            c["column"] for c in review.get("columns", [])
            if c.get("effective_verdict", {}).get("source") == "supplied"
        ]
        preview = svc.preview_verdicts(table, bundle, package)
        return {
            "table": table,
            "run_id": run_id,
            "review": review,
            "applied": applied,
            "stale_skipped": [r["column"] for r in preview["stale"]],
            "missing_skipped": [r["column"] for r in preview["missing"]],
            "conflicting": [r["column"] for r in preview["conflicting"]],
            "invalid_skipped": [r["column"] for r in preview["invalid"]],
        }

    @app.get("/review")
    def review_page(table: str = "") -> Any:
        from fastapi.responses import FileResponse
        from pathlib import Path as _Path

        static = _Path(__file__).resolve().parent / "static" / "evidence_review.html"
        if static.is_file():
            return FileResponse(static)
        raise HTTPException(status_code=404, detail="review page not found")
