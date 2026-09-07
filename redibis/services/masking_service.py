"""
redibis.services.masking_service — the Data tab orchestrator.

Ties a ScanSession's uploaded data to the masking engine: the active column
list, data preview, a saved/auto-suggested MaskingPlan, the compare preview,
and apply → safe export (+ manifest). Per-run keys live under the session's
``_meta/env`` folder and are never written into the export.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from redibis.masking.plan import MaskingPlan, auto_suggest_plan, adapt_plan_runtime, sync_plan_from_pii
from redibis.masking.engine import (
    MaskingEngine, RunKeys, risk_report, build_manifest, build_audit_report, json_scalar,
)

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_df(path: str) -> pd.DataFrame:
    if path.endswith((".parquet", ".pq")):
        return pd.read_parquet(path)
    return pd.read_csv(path)


class MaskingService:
    """Session-scoped de-identification service."""

    def __init__(self, session: Any):
        self.session = session

    # ── Paths ───────────────────────────────────────────────────────────────

    @property
    def _session_dir(self) -> Path:
        return Path(self.session.data_path).parent

    @property
    def _plan_path(self) -> Path:
        return self._session_dir / "mask_plan.yaml"

    @property
    def _env_dir(self) -> Path:
        d = self._session_dir / "_meta" / "env"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def _masked_dir(self) -> Path:
        d = self._session_dir / "masked"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _df(self) -> pd.DataFrame:
        return _load_df(self.session.data_path)

    # ── Data preview / columns ────────────────────────────────────────────────

    def preview_data(self, rows: int = 10, page: int = 0, full: bool = False,
                     row: int | None = None) -> dict:
        df = self._df()
        total = len(df)
        if row is not None:
            idx = max(0, min(row, total - 1)) if total else 0
            view = df.iloc[idx: idx + 1] if total else df.iloc[0:0]
            clean = view.where(pd.notnull(view), None)
            fields = []
            if total:
                for col in df.columns:
                    val = clean[col].iloc[0]
                    fields.append({"column": col, "value": json_scalar(val)})
            return {
                "row_index": idx,
                "total_rows": total,
                "fields": fields,
                "columns": df.columns.tolist(),
                "rows": clean.values.tolist() if total else [],
            }
        if full:
            start = max(0, page) * rows
            view = df.iloc[start:start + rows]
        else:
            view = df.head(rows)
        clean = view.where(pd.notnull(view), None)
        return {"columns": df.columns.tolist(), "rows": clean.values.tolist(),
                "total_rows": total, "total_cols": len(df.columns),
                "page": page, "page_size": rows, "full": full}

    def columns(self) -> list[dict]:
        """Active column list: include_in_scan toggle + entity badge + strategy."""
        df = self._df()
        plan = self.get_plan()
        ent = self._entity_by_column()
        out = []
        for c in df.columns:
            rule = plan.rule_for(c)
            out.append({
                "column": c,
                "include_in_scan": rule.include_in_scan if rule else True,
                "detected_entity": (rule.detected_entity if rule else None) or ent.get(c),
                "strategy": rule.strategy if rule else "passthrough",
            })
        return out

    def set_column_include(self, column: str, include: bool) -> dict:
        plan = self.get_plan()
        rule = plan.rule_for(column)
        if rule is None:
            from redibis.masking.plan import ColumnMaskRule
            rule = ColumnMaskRule(column=column)
            plan.columns.append(rule)
        rule.include_in_scan = bool(include)
        self.save_plan(plan)
        return {"column": column, "include_in_scan": rule.include_in_scan}

    # ── Plan ──────────────────────────────────────────────────────────────────

    def _configured_default_locale(self) -> str:
        """Session default first; root config fallback for older sessions."""
        from redibis.masking import transforms as T
        session_default = getattr(getattr(self.session, "common_config", None),
                                  "masking_default_locale", None)
        if session_default:
            return T.canonicalize_faker_locale(session_default)
        try:
            import os
            from redibis.config import RedibisConfig
            cfg_path = os.environ.get("REDIBIS_CONFIG")
            cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
            return T.canonicalize_faker_locale(cfg.masking.default_locale)
        except Exception:
            return "default"

    def _entity_by_column(self) -> dict:
        """Detected entity per column from the session's last PII scan."""
        out: dict[str, str] = {}
        for d in (getattr(self.session, "pii_detections", None) or []):
            col = getattr(d, "column", None) if not isinstance(d, dict) else d.get("column")
            ent = getattr(d, "entity_type", None) if not isinstance(d, dict) else d.get("entity_type")
            det = getattr(d, "detected", None) if not isinstance(d, dict) else d.get("detected")
            if col and det and ent:
                out[col] = ent
        return out

    def _detections(self) -> list[dict]:
        out = []
        for d in (getattr(self.session, "pii_detections", None) or []):
            if isinstance(d, dict):
                out.append(d)
            else:
                out.append({"column": getattr(d, "column", None),
                            "detected": getattr(d, "detected", False),
                            "entity_type": getattr(d, "entity_type", None)})
        return out

    def get_plan(self) -> MaskingPlan:
        """Load the saved plan, or auto-suggest one from the PII scan."""
        if self._plan_path.exists():
            plan = MaskingPlan.from_yaml(self._plan_path.read_text(encoding="utf-8"))
        else:
            df = self._df()
            default_locale = self._configured_default_locale()
            plan = auto_suggest_plan(self.session.table_name, df.columns.tolist(),
                                     self._detections(), default_locale=default_locale,
                                     session_id=self.session.session_id)
        df = self._df()
        changed = sync_plan_from_pii(
            plan, df.columns.tolist(), self._detections(),
            default_locale=plan.default_locale,
        )
        if adapt_plan_runtime(plan):
            changed = True
        if changed or not self._plan_path.exists():
            self.save_plan(plan)
        return plan

    def regenerate_plan(self, default_locale: Optional[str] = None) -> MaskingPlan:
        """Discard the saved plan and re-derive from the current PII scan."""
        df = self._df()
        locale = default_locale or self._configured_default_locale()
        plan = auto_suggest_plan(self.session.table_name, df.columns.tolist(),
                                 self._detections(), default_locale=locale,
                                 session_id=self.session.session_id)
        self.save_plan(plan)
        return plan

    def save_plan(self, plan: MaskingPlan | dict) -> MaskingPlan:
        if isinstance(plan, dict):
            plan = MaskingPlan.from_dict(plan)
        if not (plan.default_locale or "").strip():
            plan.default_locale = self._configured_default_locale()
        plan.updated_at = _utc_now_iso()
        plan.session_id = plan.session_id or self.session.session_id
        plan.schema_table = plan.schema_table or self.session.table_name
        self._plan_path.write_text(plan.to_yaml(), encoding="utf-8")
        # The active column list drives scan scope too (one structure, two uses).
        try:
            self.session.common_config.selected_columns = plan.scan_columns() or None
        except Exception:
            pass
        return plan

    # ── Keys (per-run, under _meta/env) ────────────────────────────────────────

    def _mint_keys(self, plan: MaskingPlan, run_id: Optional[str] = None) -> RunKeys:
        keys = RunKeys.mint(run_id=run_id, seed=plan.seed or None)
        (self._env_dir / f"{keys.run_id}.json").write_text(
            json.dumps(keys.to_secret_dict(), indent=2), encoding="utf-8")
        return keys

    def _load_keys(self, run_id: str) -> Optional[RunKeys]:
        p = self._env_dir / f"{run_id}.json"
        if not p.exists():
            return None
        return RunKeys.from_secret_dict(json.loads(p.read_text(encoding="utf-8")))

    # ── Preview / apply / export ───────────────────────────────────────────────

    def preview_mask(self, row: int = 0) -> dict:
        plan = self.get_plan()
        keys = RunKeys.mint(seed=plan.seed or "preview")  # ephemeral, not persisted
        eng = MaskingEngine(plan, keys)
        df = self._df()
        return eng.compare_record(df, row_index=row)

    def risk(self) -> list[dict]:
        return risk_report(self._df(), self.get_plan())

    def apply_export(self, fmt: str = "csv") -> dict:
        fmt = (fmt or "csv").lower()
        if fmt not in ("csv", "parquet"):
            raise ValueError("format must be csv or parquet")
        plan = self.get_plan()
        run_id = f"mask_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        plan.run_id = run_id
        keys = self._mint_keys(plan, run_id=run_id)
        plan.seed = plan.seed or keys.seed
        plan.key_refs = keys.key_refs
        self.save_plan(plan)

        df = self._df()
        eng = MaskingEngine(plan, keys)
        masked = eng.transform_dataframe(df)

        safe_table = self.session.table_name.replace(".", "_")
        ext = "parquet" if fmt == "parquet" else "csv"
        out_path = self._masked_dir / f"{safe_table}_{run_id}.{ext}"
        if fmt == "parquet":
            masked.to_parquet(out_path, index=False)
        else:
            masked.to_csv(out_path, index=False)

        manifest = build_manifest(
            plan, keys,
            column_run_meta=eng.column_run_meta,
            rows=len(df),
            session_id=self.session.session_id,
            created_at=_utc_now_iso(),
        )
        manifest_path = self._masked_dir / f"{safe_table}_{run_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                 encoding="utf-8")

        audit = build_audit_report(
            plan, keys,
            df=df,
            column_run_meta=eng.column_run_meta,
            session_id=self.session.session_id,
            created_at=_utc_now_iso(),
            export_format=fmt,
            export_filename=out_path.name,
            source_label=getattr(self.session, "data_path", None) or self.session.table_name,
        )
        audit_path = self._masked_dir / f"{safe_table}_{run_id}.audit.json"
        audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False),
                              encoding="utf-8")

        try:
            self.session.artifacts["masked_export"] = str(out_path)
            self.session.artifacts["masked_manifest"] = str(manifest_path)
            self.session.artifacts["masked_audit"] = str(audit_path)
            self.session.add_log(f"Masked export written: {out_path.name} (run {run_id})")
        except Exception:
            pass
        return {"run_id": run_id, "format": fmt, "rows": len(df),
                "export_path": str(out_path), "manifest_path": str(manifest_path),
                "audit_path": str(audit_path),
                "manifest": manifest, "audit": audit}

    def manifest(self) -> Optional[dict]:
        plan = self.get_plan()
        if not plan.run_id:
            return None
        keys = self._load_keys(plan.run_id)
        if keys is None:
            return None
        return build_manifest(
            plan, keys,
            rows=len(self._df()),
            session_id=self.session.session_id,
            created_at=_utc_now_iso(),
        )

    def audit(self, run_id: Optional[str] = None) -> Optional[dict]:
        """Load the audit report for a run (latest export if run_id omitted)."""
        path = self._audit_path(run_id)
        if path is None or not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_exports(self) -> list[dict]:
        """List all masked exports in this session (newest first)."""
        d = self._masked_dir
        if not d.exists():
            return []
        audits = sorted(d.glob("*.audit.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for ap in audits:
            try:
                audit = json.loads(ap.read_text(encoding="utf-8"))
            except Exception:
                continue
            run_id = audit.get("run_id", ap.stem.replace(".audit", ""))
            export_name = (audit.get("export") or {}).get("filename", "")
            out.append({
                "run_id": run_id,
                "created_at": audit.get("created_at"),
                "format": (audit.get("export") or {}).get("format"),
                "rows": (audit.get("source") or {}).get("rows"),
                "export_filename": export_name,
                "audit_path": str(ap),
                "summary": audit.get("summary"),
            })
        return out

    def _audit_path(self, run_id: Optional[str] = None) -> Optional[Path]:
        if run_id:
            safe_table = self.session.table_name.replace(".", "_")
            p = self._masked_dir / f"{safe_table}_{run_id}.audit.json"
            return p
        plan = self.get_plan()
        if not plan.run_id:
            return None
        safe_table = self.session.table_name.replace(".", "_")
        return self._masked_dir / f"{safe_table}_{plan.run_id}.audit.json"

    def audit_download_path(self, run_id: Optional[str] = None) -> Optional[Path]:
        p = self._audit_path(run_id)
        return p if p and p.exists() else None

    def export_path(self, fmt: str = "csv") -> Optional[Path]:
        plan = self.get_plan()
        if not plan.run_id:
            return None
        safe_table = self.session.table_name.replace(".", "_")
        ext = "parquet" if (fmt or "csv").lower() == "parquet" else "csv"
        p = self._masked_dir / f"{safe_table}_{plan.run_id}.{ext}"
        return p if p.exists() else None
