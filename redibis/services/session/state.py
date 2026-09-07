"""Session state: models, ScanSession, SessionManager, persistence."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

import pandas as pd
import yaml

from redibis.models import PIIDetection
from redibis.profiling.base import ProfileResult
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.services.scan_service import ScanConfig, ScanResult
from redibis.services.session.config import CommonConfig, GlobalConfig, _build_scan_config

log = logging.getLogger(__name__)

# ── RunRecord ─────────────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    """One scan or discovery action within a session."""
    run_id: str
    run_type: str
    started_at: str
    run_kind: str = "scan"          # "scan" | "discovery" — the two run kinds
    completed_at: Optional[str] = None
    status: str = "running"
    error: Optional[str] = None
    config_snapshot: Dict[str, Any] = field(default_factory=dict)
    pii_confirmed: int = 0
    pii_signals: int = 0
    pii_total_scanned: int = 0
    quality_passed: int = 0
    quality_failed: int = 0
    quality_total: int = 0
    pii_detections: List[dict] = field(default_factory=list)
    quality_results: List[dict] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)

    def add_log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")

    def finish(self, status: str = "complete", error: Optional[str] = None):
        self.status = status
        self.completed_at = datetime.now(timezone.utc).isoformat()
        if error:
            self.error = error

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            try:
                s = datetime.fromisoformat(self.started_at)
                e = datetime.fromisoformat(self.completed_at)
                return round((e - s).total_seconds(), 2)
            except Exception:
                return None
        return None

    def to_dict(self) -> dict:
        return {**asdict(self), "duration_seconds": self.duration_seconds}

# ── SubContract ───────────────────────────────────────────────────────────────

@dataclass
class SubContract:
    """
    A staged partial ODCS contract (PII or Quality) produced by a scan or
    added/edited by hand. Sub-contracts are NOT auto-merged in the web flow;
    the user adds / deletes / edits them, then explicitly merges the ones
    they're satisfied with into the active contract via ContractStore.upsert().
    """
    sub_id: str
    kind: str                       # "pii" | "quality" | "business" | "manual"
    table: str
    content: Dict[str, Any] = field(default_factory=dict)
    run_id: Optional[str] = None
    source: str = "scan"            # "scan" | "manual" | "upload"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Optional[str] = None
    status: str = "staged"          # "staged" | "merged"
    merged_version: Optional[str] = None

    def summary(self) -> dict:
        """Lightweight view (no full ODCS content) for list rendering."""
        return {
            "sub_id": self.sub_id, "kind": self.kind, "table": self.table,
            "run_id": self.run_id, "source": self.source, "status": self.status,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "merged_version": self.merged_version,
            "property_count": _odcs_property_count(self.content),
        }

    def to_dict(self) -> dict:
        return {**self.summary(), "content": self.content}


def _odcs_property_count(contract: dict) -> int:
    """Count schema properties in an ODCS partial (best-effort)."""
    try:
        return sum(len(s.get("properties", [])) for s in (contract.get("schema") or []))
    except Exception:
        return 0
# ── Approved basket (cherry-picked PII columns + quality rules) ───────────────

@dataclass
class ApprovedProperty:
    """
    One reviewer-approved contract fragment cherry-picked from an interactive
    report. Either a single column's PII block (kind="pii") or a single quality
    rule targeting a column / the table (kind="quality").
    """
    prop_id: str
    kind: str                                  # "pii" | "quality" | "glossary"
    column: str                                # target column ("" => table-level quality)
    source_run_id: Optional[str] = None
    source: str = "scan"                        # "scan" | "discovery" | "manual"
    label: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    status: str = "approved"                    # "approved" | "merged"
    note: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Optional[str] = None
    merged_version: Optional[str] = None

    def summary(self) -> dict:
        return {
            "prop_id": self.prop_id, "kind": self.kind, "column": self.column,
            "source_run_id": self.source_run_id, "source": self.source,
            "label": self.label, "status": self.status, "note": self.note,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "merged_version": self.merged_version,
        }

    def to_dict(self) -> dict:
        return {**self.summary(), "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict) -> "ApprovedProperty":
        return cls(
            prop_id=d.get("prop_id") or f"ap_{uuid.uuid4().hex[:12]}",
            kind=d.get("kind", ""), column=d.get("column", ""),
            source_run_id=d.get("source_run_id"), source=d.get("source", "scan"),
            label=d.get("label", ""), payload=d.get("payload", {}) or {},
            status=d.get("status", "approved"), note=d.get("note", ""),
            created_at=d.get("created_at") or datetime.now(timezone.utc).isoformat(),
            updated_at=d.get("updated_at"), merged_version=d.get("merged_version"),
        )


@dataclass
class ApprovedSet:
    """Session-wide ordered basket of approved properties (de-duped by identity)."""
    items: List[ApprovedProperty] = field(default_factory=list)

    @staticmethod
    def _identity(kind: str, column: str, payload: dict) -> str:
        if kind == "pii":
            return f"pii::{column}"
        if kind == "glossary":
            return f"glossary::{column}"
        impl = payload.get("implementation") or {}
        sig = (payload.get("rule") or payload.get("type")
               or impl.get("expectation_type")
               or payload.get("name"))
        if not sig:
            sig = hashlib.md5(
                json.dumps({k: v for k, v in payload.items() if k != "description"},
                           sort_keys=True, default=str).encode(),
            ).hexdigest()[:10]
        return f"quality::{column}::{sig}"

    def add(self, prop: ApprovedProperty) -> ApprovedProperty:
        ident = self._identity(prop.kind, prop.column, prop.payload)
        for i, existing in enumerate(self.items):
            if self._identity(existing.kind, existing.column, existing.payload) == ident:
                prop.prop_id = existing.prop_id
                prop.created_at = existing.created_at
                prop.updated_at = datetime.now(timezone.utc).isoformat()
                self.items[i] = prop
                return prop
        self.items.append(prop)
        return prop

    def get(self, prop_id: str) -> Optional[ApprovedProperty]:
        return next((p for p in self.items if p.prop_id == prop_id), None)

    def update(self, prop_id: str, *, payload=None, note=None,
               column=None, label=None) -> Optional[ApprovedProperty]:
        p = self.get(prop_id)
        if p is None:
            return None
        if payload is not None:
            p.payload = payload
        if note is not None:
            p.note = note
        if column is not None:
            p.column = column
        if label is not None:
            p.label = label
        p.updated_at = datetime.now(timezone.utc).isoformat()
        return p

    def remove(self, prop_id: str) -> bool:
        n = len(self.items)
        self.items = [p for p in self.items if p.prop_id != prop_id]
        return len(self.items) < n

    def clear(self, only_merged: bool = False) -> int:
        before = len(self.items)
        self.items = [p for p in self.items if p.status != "merged"] if only_merged else []
        return before - len(self.items)

    def by_kind(self, kind: str) -> List[ApprovedProperty]:
        return [p for p in self.items if p.kind == kind]

    def summary(self) -> dict:
        return {
            "total": len(self.items),
            "pii": len(self.by_kind("pii")),
            "quality": len(self.by_kind("quality")),
            "merged": sum(1 for p in self.items if p.status == "merged"),
            "approved": sum(1 for p in self.items if p.status == "approved"),
        }

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "items": [p.to_dict() for p in self.items]}

    @classmethod
    def from_dict(cls, d: dict) -> "ApprovedSet":
        return cls(items=[ApprovedProperty.from_dict(x) for x in (d.get("items") or [])])
# ── ScanSession ───────────────────────────────────────────────────────────────

@dataclass
class ScanSession:
    session_id: str
    table_name: str
    data_path: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "initialized"
    common_config: GlobalConfig = field(default_factory=GlobalConfig)
    config: Optional[ScanConfig] = None
    runs: List[RunRecord] = field(default_factory=list)
    sub_contracts: List[SubContract] = field(default_factory=list)
    approved: ApprovedSet = field(default_factory=ApprovedSet)
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    logs: list = field(default_factory=list)
    profiler: Optional[Union[ProfileResult, Any]] = None
    quality_gatekeeper: Optional[QualityGatekeeper] = None
    pii_detections: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    quality_contract_version: Optional[str] = None
    pii_contract_version: Optional[str] = None
    schema_contract_version: Optional[str] = None
    total_columns: int = 0
    discovery: Optional["DiscoverySession"] = None
    scan_complete: bool = False
    quality_passed: int = 0
    quality_total: int = 0
    pii_detected_count: int = 0
    source: str = "scan"
    _dataframe: Optional[Any] = None
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def global_config(self) -> GlobalConfig:
        """Preferred name for the session's single global config object."""
        return self.common_config

    # ── Editable lists live on the global config; these expose the legacy
    #    draft shape that older callers / the frontend read. ──────────────
    @property
    def quality_rules_draft(self) -> Optional[List[dict]]:
        rs = self.common_config.quality_rule_set
        return rs.rules if rs and rs.rules else None

    @property
    def pii_regex_draft(self) -> Optional[dict]:
        rs = self.common_config.regex_set
        return rs.to_dict() if rs else None

    def add_log(self, message: str, level: str = ""):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        self.logs.append(entry)
        log.info(entry)
        try:
            self.event_queue.put_nowait({"type": "log", "message": entry})
        except Exception:
            pass

    def set_status(self, new_status: str):
        self.status = new_status
        self.touch()
        try:
            self.event_queue.put_nowait({"type": "status", "status": new_status})
        except Exception:
            pass

    def start_run(self, run_type: str, config_override: Optional[dict] = None,
                  run_kind: Optional[str] = None) -> RunRecord:
        run_id = f"{run_type}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:6]}"
        snapshot = self.common_config.to_dict()
        if config_override:
            snapshot.update(config_override)
        # Freeze capability routing for this run (Settings changes apply to new runs only).
        if "capability_routing" not in snapshot:
            try:
                from redibis.config import load_global_settings_optional
                from redibis.enrich.capability_routing import capture_routing_snapshot

                snap = capture_routing_snapshot(load_global_settings_optional())
                snapshot["capability_routing"] = snap.to_dict()
            except Exception:
                pass
        kind = run_kind or ("discovery" if run_type.startswith("discovery") else "scan")
        run = RunRecord(run_id=run_id, run_type=run_type, run_kind=kind,
                        started_at=datetime.now(timezone.utc).isoformat(),
                        config_snapshot=snapshot)
        self.runs.append(run)
        self.add_log(f"Run started: {run_id} (kind={kind}, type={run_type})")
        return run

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        return next((r for r in self.runs if r.run_id == run_id), None)

    # ── Sub-contract registry (staged partials, manual merge) ──────────────
    def add_sub_contract(self, kind: str, content: dict,
                         table: Optional[str] = None, run_id: Optional[str] = None,
                         source: str = "scan") -> SubContract:
        sc = SubContract(
            sub_id=f"sc_{kind}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:6]}",
            kind=kind, table=table or self.table_name, content=content or {},
            run_id=run_id, source=source,
        )
        self.sub_contracts.append(sc)
        self.add_log(f"Sub-contract staged: {sc.sub_id} (kind={kind}, source={source})")
        return sc

    def get_sub_contract(self, sub_id: str) -> Optional[SubContract]:
        return next((s for s in self.sub_contracts if s.sub_id == sub_id), None)

    def update_sub_contract(self, sub_id: str, content: dict) -> Optional[SubContract]:
        sc = self.get_sub_contract(sub_id)
        if sc is None:
            return None
        sc.content = content
        sc.updated_at = datetime.now(timezone.utc).isoformat()
        sc.status = "staged"
        sc.merged_version = None
        return sc

    def remove_sub_contract(self, sub_id: str) -> bool:
        before = len(self.sub_contracts)
        self.sub_contracts = [s for s in self.sub_contracts if s.sub_id != sub_id]
        return len(self.sub_contracts) < before

    def approve_property(self, *, kind: str, column: str, payload: dict,
                         source_run_id: Optional[str] = None, source: str = "scan",
                         label: str = "", note: str = "") -> ApprovedProperty:
        prop = ApprovedProperty(
            prop_id=f"ap_{kind}_{uuid.uuid4().hex[:10]}",
            kind=kind, column=column, payload=payload or {},
            source_run_id=source_run_id, source=source, label=label, note=note,
        )
        p = self.approved.add(prop)
        self.add_log(f"Approved {kind} property: {column or '(table)'} ({label})")
        return p

    def latest_run(self, run_type: Optional[str] = None) -> Optional[RunRecord]:
        candidates = [r for r in self.runs if run_type is None or r.run_type == run_type]
        return candidates[-1] if candidates else None

    def prepare_for_api(self) -> None:
        """Fill gaps after rehydrate/handoff so API clients see quality artifacts + results."""
        normalize_session_artifacts(self)
        enrich_runs_from_disk(self)

    def to_dict(self) -> dict:
        pii_det_summary = []
        for d in self.pii_detections:
            pii_det_summary.append({
                "column": d.column, "detected": d.detected,
                "entity_type": d.entity_type, "confidence": d.confidence,
                "presidio_score": d.presidio_score, "gliner_score": d.gliner_score,
                "llm_score": d.llm_score, "arabic_aware": d.arabic_aware,
                "arabic_fraction": d.arabic_fraction, "triage_score": d.triage_score,
                "decision_path": d.decision_path, "presidio_pattern": d.presidio_pattern,
                "gliner_label": d.gliner_label, "llm_verdict": d.llm_verdict,
                "llm_reasoning": d.llm_reasoning, "sample_match_rate": d.sample_match_rate,
            })
        return {
            "session_id": self.session_id, "table_name": self.table_name,
            "created_at": self.created_at, "status": self.status,
            "common_config": self.common_config.to_dict(),
            "runs": [r.to_dict() for r in self.runs],
            "sub_contracts": [s.summary() for s in self.sub_contracts],
            "approved": self.approved.to_dict(),
            "discovery": self.discovery.to_dict() if self.discovery else None,
            "logs": self.logs[-100:], "artifacts": self.artifacts,
            "quality_contract_version": self.quality_contract_version,
            "pii_contract_version": self.pii_contract_version,
            "schema_contract_version": self.schema_contract_version,
            "quality_rules_draft": self.quality_rules_draft,
            "pii_regex_draft": self.pii_regex_draft,
            "total_columns": self.total_columns,
            "pii_detections": pii_det_summary,
            "quality_passed": self.quality_passed,
            "quality_total": self.quality_total,
            "pii_detected_count": self.pii_detected_count,
            "source": self.source,
        }

    def to_debug_dict(self) -> dict:
        base = self.to_dict()
        cc = self.common_config
        base["_debug"] = {
            "has_profiler": self.profiler is not None,
            "has_gatekeeper": self.quality_gatekeeper is not None,
            "raw_pii_detection_count": len(self.pii_detections),
            "run_count": len(self.runs),
            "scan_run_count": sum(1 for r in self.runs if r.run_kind == "scan"),
            "discovery_run_count": sum(1 for r in self.runs if r.run_kind == "discovery"),
            "run_kinds": [{"run_id": r.run_id, "run_kind": r.run_kind,
                           "run_type": r.run_type, "status": r.status} for r in self.runs],
            "regex_set": {"name": cc.regex_set.name,
                          "replace_all": cc.regex_set.replace_all,
                          "pattern_count": len(cc.regex_set)},
            "quality_rule_set": {"name": cc.quality_rule_set.name,
                                 "rule_count": len(cc.quality_rule_set)},
            "has_discovery": self.discovery is not None,
            "discovery_run_count_scoped": (
                len(self.discovery.runs) if self.discovery else 0),
            "sub_contract_count": len(self.sub_contracts),
            "sub_contract_staged": sum(1 for s in self.sub_contracts if s.status == "staged"),
            "sub_contract_merged": sum(1 for s in self.sub_contracts if s.status == "merged"),
            "approved_count": len(self.approved.items),
            "approved_summary": self.approved.summary(),
            "data_path": self.data_path,
            "session_dir": str(Path(self.data_path).parent),
        }
        return base

    def persist_to_disk(self) -> Path:
        session_dir = Path(self.data_path).parent
        session_dir.mkdir(parents=True, exist_ok=True)
        with open(session_dir / "session.json", "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        with open(session_dir / "session_config.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "session_id": self.session_id,
                "table_name": self.table_name,
                "created_at": self.created_at,
                "config": self.common_config.to_dict(),
            }, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        # Persist the discovery session alongside the scan session (kept
        # separate, but flushed under the same folder).
        if self.discovery is not None:
            try:
                with open(session_dir / "discovery.json", "w", encoding="utf-8") as f:
                    json.dump(self.discovery.to_dict(), f, indent=2, default=str)
            except Exception as e:
                log.warning("Failed to persist discovery session: %s", e)
        (session_dir / "approved.json").write_text(
            json.dumps(self.approved.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Staged sub-contracts: index + one YAML per partial (editable on disk).
        if self.sub_contracts:
            sub_dir = session_dir / "sub_contracts"
            sub_dir.mkdir(exist_ok=True)
            with open(sub_dir / "_index.json", "w", encoding="utf-8") as f:
                json.dump([s.summary() for s in self.sub_contracts], f, indent=2, default=str)
            for sc in self.sub_contracts:
                try:
                    with open(sub_dir / f"{sc.sub_id}.yaml", "w", encoding="utf-8") as f:
                        yaml.safe_dump(sc.content, f, default_flow_style=False,
                                       sort_keys=False, allow_unicode=True)
                except Exception as e:
                    log.warning("Failed to persist sub-contract %s: %s", sc.sub_id, e)
        runs_dir = session_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        for run in self.runs:
            run_dir = runs_dir / run.run_id
            run_dir.mkdir(exist_ok=True)
            manifest = {k: v for k, v in run.to_dict().items()
                        if k not in ("pii_detections", "quality_results", "config_snapshot")}
            with open(run_dir / "run_manifest.json", "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, default=str)
            # Full per-run config snapshot — incl. the embedded regex/quality
            # lists — so every run is independently reproducible from disk.
            snap = run.config_snapshot or {}
            with open(run_dir / "config_snapshot.json", "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, default=str)
            if snap.get("regex_set"):
                with open(run_dir / "regex_set.yaml", "w", encoding="utf-8") as f:
                    yaml.safe_dump(snap["regex_set"], f, default_flow_style=False,
                                   sort_keys=False, allow_unicode=True)
            if snap.get("quality_rule_set"):
                with open(run_dir / "quality_rule_set.yaml", "w", encoding="utf-8") as f:
                    yaml.safe_dump(snap["quality_rule_set"], f, default_flow_style=False,
                                   sort_keys=False, allow_unicode=True)
            if run.pii_detections:
                with open(run_dir / "pii_detections.json", "w", encoding="utf-8") as f:
                    json.dump(run.pii_detections, f, indent=2, default=str)
            if run.quality_results:
                with open(run_dir / "quality_results.json", "w", encoding="utf-8") as f:
                    json.dump(run.quality_results, f, indent=2, default=str)
        self.add_log(f"Session flushed to disk: {session_dir}")
        return session_dir

    def get_sample(self, rows: int = 10) -> dict:
        df = _load_dataframe(self)
        df_clean = df.head(rows).where(pd.notnull(df), None)
        return {"columns": df.columns.tolist(), "rows": df_clean.values.tolist(),
                "total_rows": len(df), "total_cols": len(df.columns)}

# ── Session rehydration (scan_output on disk) ────────────────────────────────

_RUN_RECORD_KEYS = {f.name for f in fields(RunRecord)}

# Legacy / handoff artifact keys → canonical keys used by the web UI + artifact API.
_ARTIFACT_KEY_ALIASES = {
    "quality_contract.yaml": "quality_contract",
    "pii_contract.yaml": "pii_contract",
    "interactive_review.html": "interactive_review",
    "triage_report.html": "triage_report",
    "pii_regex_review.html": "pii_regex_review",
    "pii_detections.html": "pii_detection_report",
}

_ARTIFACT_FILENAMES = {
    "quality_contract": "quality_contract.yaml",
    "pii_contract": "pii_contract.yaml",
    "interactive_review": "interactive_review.html",
    "triage_report": "triage_report.html",
    "pii_regex_review": "pii_regex_review.html",
    "pii_detection_report": "pii_detections.html",
    "run_report": "run_report.html",
}


def normalize_session_artifacts(session: ScanSession) -> None:
    """Canonicalize artifact map keys and discover files under the session folder."""
    session_dir = Path(session.data_path).parent
    art = session.artifacts

    for old_key, new_key in _ARTIFACT_KEY_ALIASES.items():
        if old_key in art and new_key not in art:
            art[new_key] = art.pop(old_key)

    if art.get("pii_detections_html") and "pii_detection_report" not in art:
        art["pii_detection_report"] = art["pii_detections_html"]

    for key, fname in _ARTIFACT_FILENAMES.items():
        if key in art and art[key] and Path(str(art[key])).is_file():
            continue
        candidate = session_dir / fname
        if candidate.is_file():
            art[key] = str(candidate)

    if "quality_report" not in art:
        reports = sorted(session_dir.glob("quality-report-*.html"))
        if reports:
            art["quality_report"] = str(reports[-1])

    ge_index = session_dir / "ge_report" / "index.html"
    if "ge_report" not in art and ge_index.is_file():
        art["ge_report"] = str(ge_index)


def enrich_runs_from_disk(session: ScanSession) -> None:
    """Restore per-run quality results + stats when session.json is incomplete."""
    session_dir = Path(session.data_path).parent
    runs_dir = session_dir / "runs"
    if not runs_dir.is_dir():
        return

    by_id = {r.run_id: r for r in session.runs}

    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        rid = run_dir.name
        run = by_id.get(rid)

        manifest: dict = {}
        manifest_path = run_dir / "run_manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}

        if run is None and manifest:
            run = RunRecord(
                run_id=rid,
                run_type=str(manifest.get("run_type") or "scan_quality"),
                started_at=str(manifest.get("started_at") or ""),
                run_kind=str(manifest.get("run_kind") or "scan"),
                status=str(manifest.get("status") or "complete"),
            )
            session.runs.append(run)
            by_id[rid] = run

        qpath = run_dir / "quality_results.json"
        if not qpath.is_file() or run is None:
            continue
        try:
            qdata = json.loads(qpath.read_text(encoding="utf-8"))
        except Exception:
            qdata = []
        if not isinstance(qdata, list):
            continue
        if not run.quality_results:
            run.quality_results = qdata
        if run.run_type in ("unified", "") and run.quality_results:
            run.run_type = "scan_quality"
        if run.quality_results and not run.quality_total:
            run.quality_total = len(run.quality_results)
            run.quality_passed = sum(
                1 for row in run.quality_results if row.get("success")
            )
            run.quality_failed = run.quality_total - run.quality_passed

    for run in reversed(session.runs):
        if run.quality_total:
            session.quality_passed = run.quality_passed
            session.quality_total = run.quality_total
            break


def _pii_detection_from_dict(d: dict) -> PIIDetection:
    valid = {f.name for f in fields(PIIDetection)}
    return PIIDetection(**{k: v for k, v in (d or {}).items() if k in valid})


def rehydrate_scan_session(session_dir: Path, output_dir: Path) -> Optional[ScanSession]:
    """Rebuild an in-memory ScanSession from ``scan_output/<id>/session.json``."""
    state_file = session_dir / "session.json"
    if not state_file.exists():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    session_id = state.get("session_id") or session_dir.name
    table_name = state.get("table_name")
    if not table_name:
        cfg_file = session_dir / "session_config.yaml"
        if cfg_file.exists():
            table_name = (yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}).get("table_name")
    if not table_name:
        return None

    data_path = session_dir / "data.csv"
    if not data_path.exists():
        alt = session_dir / "session_data"
        data_path = alt if alt.exists() else data_path

    cc = (GlobalConfig.from_dict(state["common_config"])
          if state.get("common_config") else GlobalConfig())
    session = ScanSession(
        session_id=session_id, table_name=table_name,
        data_path=str(data_path), common_config=cc,
    )
    session.created_at = state.get("created_at") or session.created_at
    session.status = state.get("status") or session.status
    session.logs = list(state.get("logs") or [])
    session.artifacts = dict(state.get("artifacts") or {})
    session.quality_contract_version = state.get("quality_contract_version")
    session.pii_contract_version = state.get("pii_contract_version")
    session.schema_contract_version = state.get("schema_contract_version")
    session.total_columns = int(state.get("total_columns") or 0)
    session.quality_passed = int(state.get("quality_passed") or 0)
    session.quality_total = int(state.get("quality_total") or 0)
    session.pii_detected_count = int(state.get("pii_detected_count") or 0)
    session.source = str(state.get("source") or "scan")
    session.scan_complete = session.status in (
        "scan_complete", "pii_complete", "quality_complete",
    )
    session.runs = [
        RunRecord(**{k: v for k, v in r.items() if k in _RUN_RECORD_KEYS})
        for r in (state.get("runs") or [])
    ]
    session.pii_detections = [
        _pii_detection_from_dict(d) for d in (state.get("pii_detections") or [])
    ]
    approved_file = session_dir / "approved.json"
    if approved_file.exists():
        session.approved = ApprovedSet.from_dict(
            json.loads(approved_file.read_text(encoding="utf-8")))
    elif state.get("approved"):
        session.approved = ApprovedSet.from_dict(state["approved"])

    disc_file = session_dir / "discovery.json"
    if disc_file.exists():
        try:
            from redibis.services.discovery_service import DiscoverySession
            disc_state = json.loads(disc_file.read_text(encoding="utf-8"))
            session.discovery = DiscoverySession(
                discovery_id=disc_state.get("discovery_id") or "",
                created_at=disc_state.get("created_at") or "",
            )
            from redibis.services.discovery_service import DiscoveryRun
            session.discovery.runs = [
                DiscoveryRun(**{k: v for k, v in r.items()
                              if k in {f.name for f in fields(DiscoveryRun)}})
                for r in (disc_state.get("runs") or [])
            ]
        except Exception as e:
            log.warning("Failed to rehydrate discovery for %s: %s", session_id, e)

    session.config = _build_scan_config(table_name, cc, output_dir)
    session.prepare_for_api()
    session.add_log(f"Session rehydrated from disk: {session_dir}")
    return session


def _session_list_entry(state: dict, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "table_name": state.get("table_name"),
        "status": state.get("status"),
        "created_at": state.get("created_at"),
        "run_count": len(state.get("runs") or []),
        "pii_detected_count": state.get("pii_detected_count") or 0,
        "quality_passed": state.get("quality_passed") or 0,
        "quality_total": state.get("quality_total") or 0,
        "persisted": True,
    }


# ── SessionManager ────────────────────────────────────────────────────────────

class SessionManager:
    def __init__(
        self,
        max_sessions: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
    ):
        self.max_sessions = max_sessions or int(os.getenv("REDIBIS_MAX_SESSIONS", "200"))
        self.ttl_seconds = ttl_seconds or int(os.getenv("REDIBIS_SESSION_TTL", "21600"))
        self._sessions: OrderedDict[str, ScanSession] = OrderedDict()

    def _output_dir(self) -> Path:
        return Path(os.getenv("SCAN_OUTPUT_DIR", "./scan_output"))

    def _evict(self) -> None:
        now = time.time()
        expired = [
            sid for sid, sess in self._sessions.items()
            if now - getattr(sess, "last_used", now) > self.ttl_seconds
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    def create_session(self, table_name: str, file_bytes: bytes,
                       output_dir: Path, common_config: Optional[CommonConfig] = None) -> ScanSession:
        self._evict()
        session_id = str(uuid.uuid4())
        run_dir = output_dir / session_id
        run_dir.mkdir(parents=True, exist_ok=True)
        data_path = run_dir / "data.csv"
        with open(data_path, "wb") as f:
            f.write(file_bytes)
        cc = common_config or CommonConfig()
        session = ScanSession(session_id=session_id, table_name=table_name,
                              data_path=str(data_path), common_config=cc)
        session.config = _build_scan_config(table_name, cc, output_dir)
        session.touch()
        self._sessions[session_id] = session
        self._sessions.move_to_end(session_id)
        # Persist immediately so reload / another worker can rehydrate mid-scan.
        try:
            session.persist_to_disk()
        except Exception as exc:
            log.warning("Initial session persist failed for %s: %s", session_id, exc)
        self._evict()
        return session

    def peek_session(self, session_id: str) -> Optional[ScanSession]:
        """In-memory lookup only (no disk rehydrate)."""
        return self._sessions.get(session_id)

    def get_session(self, session_id: str) -> Optional[ScanSession]:
        self._evict()
        session = self._sessions.get(session_id)
        if session is not None:
            session.touch()
            self._sessions.move_to_end(session_id)
            return session
        output_dir = self._output_dir()
        session_dir = output_dir / session_id
        session = rehydrate_scan_session(session_dir, output_dir)
        if session is not None:
            session.touch()
            self._sessions[session_id] = session
            self._sessions.move_to_end(session_id)
            self._evict()
        return session

    def register(self, session: ScanSession) -> None:
        """Cache a rehydrated session in the live registry."""
        session.touch()
        self._sessions[session.session_id] = session
        self._sessions.move_to_end(session.session_id)
        self._evict()

    def get_or_load(self, session_id: str,
                    output_dir: Optional[Path] = None) -> Optional[ScanSession]:
        """Return a live session, rehydrating from ``scan_output`` when needed."""
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        if output_dir is None:
            return None
        session_dir = output_dir / session_id
        session = rehydrate_scan_session(session_dir, output_dir)
        if session is not None:
            self._sessions[session_id] = session
        return session

    def list_sessions(self, output_dir: Optional[Path] = None) -> list[dict]:
        entries: dict[str, dict] = {}
        for s in self._sessions.values():
            entries[s.session_id] = {
                "session_id": s.session_id, "table_name": s.table_name,
                "status": s.status, "created_at": s.created_at,
                "run_count": len(s.runs), "pii_detected_count": s.pii_detected_count,
                "quality_passed": s.quality_passed, "quality_total": s.quality_total,
                "persisted": False,
            }
        if output_dir is not None and output_dir.is_dir():
            for child in output_dir.iterdir():
                if not child.is_dir() or child.name in entries:
                    continue
                state_file = child / "session.json"
                if not state_file.exists():
                    continue
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    sid = state.get("session_id") or child.name
                    entries[sid] = _session_list_entry(state, sid)
                except Exception:
                    continue
        return sorted(
            entries.values(),
            key=lambda x: x.get("created_at") or "",
            reverse=True,
        )

    def delete_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False


session_manager = SessionManager()
def _load_dataframe(session: ScanSession) -> pd.DataFrame:
    p = session.data_path
    if p.endswith(".csv"): return pd.read_csv(p)
    if p.endswith((".parquet", ".pq")): return pd.read_parquet(p)
    return pd.read_csv(p)

def split_table(table: str) -> tuple[str, str]:
    parts = table.split(".")
    return ("default", parts[0]) if len(parts) == 1 else (parts[0], parts[1])
def load_session_from_dir(path: str) -> "ScanSession":
    """Rehydrate a ScanSession from a persisted session directory.

    Restores enough state for the approved-basket flow (identity, global config,
    basket). Heavy runtime objects (profiler, gatekeeper, dataframe) are NOT
    restored. ``data_path`` is set so a subsequent ``persist_to_disk()`` writes
    back into the same directory.
    """
    session_dir = Path(path)
    if not session_dir.exists():
        raise FileNotFoundError(f"Session directory not found: {path}")
    if not session_dir.is_dir():
        session_dir = session_dir.parent

    state: Dict[str, Any] = {}
    state_file = session_dir / "session.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))

    session_id = state.get("session_id") or session_dir.name
    table_name = state.get("table_name")
    if not table_name:
        cfg_file = session_dir / "session_config.yaml"
        if cfg_file.exists():
            table_name = (yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}).get("table_name")
    if not table_name:
        raise ValueError(f"Cannot determine table_name for session at {session_dir}")

    common_config = (GlobalConfig.from_dict(state["common_config"])
                     if state.get("common_config") else GlobalConfig())

    session = ScanSession(
        session_id=session_id, table_name=table_name,
        data_path=str(session_dir / "session_data"),
        common_config=common_config,
    )

    approved_file = session_dir / "approved.json"
    if approved_file.exists():
        session.approved = ApprovedSet.from_dict(
            json.loads(approved_file.read_text(encoding="utf-8")))
    elif state.get("approved"):
        session.approved = ApprovedSet.from_dict(state["approved"])
    return session
async def sse_log_generator(
    session: ScanSession,
    *,
    ping_timeout: float = 30.0,
    request: Any = None,
    max_duration: float = 7200.0,
) -> AsyncGenerator[str, None]:
    start = time.monotonic()
    for msg in session.logs:
        yield f"data: {json.dumps({'type':'log','message':msg})}\n\n"
    while True:
        if request is not None and await request.is_disconnected():
            return
        if time.monotonic() - start > max_duration:
            return
        try:
            event = await asyncio.wait_for(session.event_queue.get(), timeout=ping_timeout)
            yield f"data: {json.dumps(event)}\n\n"
        except asyncio.TimeoutError:
            yield ": ping\n\n"
