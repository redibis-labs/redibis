"""Attach steward verdict files to scan / enrich so human-verified columns lock.

A path may be one A1 / ``redibis.verdict_package`` file or a directory of them.
Only rows whose **table** and **schema column** match the current run are
considered. Human-verified PII (accept / edit / no_action, or a portable
``pii`` / ``not_pii`` entry) is written to the overlay and later scan / LLM
merges skip those columns. ``needs_review``, ``reject``, missing columns, other
tables, and fingerprint-stale rows stay with the engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from redibis.review.verdict_package import (
    STEWARD_VERDICTS_KIND,
    VerdictEntry,
    VerdictPackage,
    VerdictPackageError,
    load_verdict_package,
)
from redibis.store.pii_decisions import PiiDecision
from redibis.store.review_store import REVIEWED_DECISIONS, REVIEWED_STATUSES

STEWARD_VERDICT_ACTOR_PREFIX = "steward-verdict:"
_VERDICT_SUFFIXES = {".json", ".jsonl"}


class StewardAttachError(ValueError):
    """Invalid steward-verdict path or payload."""


@dataclass
class AttachReport:
    table: str
    path: str
    applied: list[str] = field(default_factory=list)
    skipped_needs_review: list[str] = field(default_factory=list)
    skipped_other_table: list[str] = field(default_factory=list)
    skipped_schema: list[str] = field(default_factory=list)
    skipped_stale: list[str] = field(default_factory=list)
    skipped_pending: list[str] = field(default_factory=list)
    definitions_applied: list[str] = field(default_factory=list)
    files_loaded: list[str] = field(default_factory=list)
    overlay_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "path": self.path,
            "applied": list(self.applied),
            "skipped_needs_review": list(self.skipped_needs_review),
            "skipped_other_table": list(self.skipped_other_table),
            "skipped_schema": list(self.skipped_schema),
            "skipped_stale": list(self.skipped_stale),
            "skipped_pending": list(self.skipped_pending),
            "definitions_applied": list(self.definitions_applied),
            "files_loaded": list(self.files_loaded),
            "overlay_applied": self.overlay_applied,
        }


def iter_verdict_files(path: Path | str) -> list[Path]:
    """Resolve a file or a directory of ``.json`` / ``.jsonl`` verdict files."""
    p = Path(path)
    if p.is_file():
        return [p]
    if not p.is_dir():
        raise StewardAttachError(f"steward verdict path not found: {p}")
    found = sorted(
        {q.resolve() for q in p.iterdir() if q.is_file() and q.suffix.lower() in _VERDICT_SUFFIXES}
    )
    if not found:
        raise StewardAttachError(f"no .json / .jsonl steward verdict files in {p}")
    return found


def load_steward_verdict_path(path: Path | str) -> list[tuple[Path, VerdictPackage, Optional[dict]]]:
    """Load every package at *path*. Raw A1 dicts are kept for definition rows."""
    loaded: list[tuple[Path, VerdictPackage, Optional[dict]]] = []
    for file in iter_verdict_files(path):
        try:
            package = load_verdict_package(file)
        except VerdictPackageError as exc:
            raise StewardAttachError(str(exc)) from exc
        raw: Optional[dict] = None
        if file.suffix.lower() != ".jsonl":
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and data.get("kind") == STEWARD_VERDICTS_KIND:
                raw = data
        loaded.append((file, package, raw))
    return loaded


def _verdict_table_matches(entry: VerdictEntry, package: VerdictPackage, raw: Optional[dict], table: str) -> bool:
    if entry.table:
        return entry.table == table
    hint = str((raw or {}).get("table") or "")
    if hint:
        return hint == table
    tables = list(package.tables or [])
    if tables:
        return table in tables
    return True


def _schema_names(store, table: str, extra: Optional[Iterable[str]] = None) -> set[str]:
    names: set[str] = set()
    try:
        names.update(str(n) for n in (store.column_names(table) or []) if n)
    except Exception:
        pass
    if extra:
        names.update(str(n) for n in extra if n)
    return names


def _fingerprints_from_store(store, table: str) -> dict[str, str]:
    fps: dict[str, str] = {}
    active = None
    try:
        active = store.get_active(table)
    except Exception:
        active = None
    if not isinstance(active, dict):
        return fps
    from redibis.review.fingerprint import fingerprint_from_contract_prop

    for schema_obj in active.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict) or not prop.get("name"):
                continue
            name = str(prop["name"])
            fps[name] = fingerprint_from_contract_prop(name, prop).fingerprint_key
    return fps


def _payload_for(entry: VerdictEntry) -> dict[str, Any]:
    if entry.status != "pii":
        return {}
    pii: dict[str, Any] = {"detected": True}
    if entry.entity_type:
        pii["entity_type"] = entry.entity_type
    return {"pii": pii}


def locked_human_pii_columns(store, table: str) -> set[str]:
    """Columns whose human-verified PII must win over deterministic / LLM output."""
    locked: set[str] = set()
    try:
        decisions = store.get_pii_decisions(table) or {}
    except Exception:
        decisions = {}
    for col, raw in decisions.items():
        if not col or not isinstance(raw, dict):
            continue
        lifecycle = str(raw.get("lifecycle_state") or "active").lower()
        if lifecycle in ("stale", "superseded"):
            continue
        who = str(raw.get("decided_by") or "")
        if who.startswith(STEWARD_VERDICT_ACTOR_PREFIX) or who.startswith("import:"):
            locked.add(str(col))

    try:
        from redibis.store.review_store import ReviewStore

        reviews = ReviewStore(store.backend, store.bucket)
        state = reviews.get(table)
    except Exception:
        return locked

    for col, cr in (state.columns or {}).items():
        fv = (cr.verdicts or {}).get("pii")
        decision = ""
        if fv is not None:
            decision = getattr(fv, "decision", None) or (fv.get("decision") if isinstance(fv, dict) else "")
        if decision in REVIEWED_DECISIONS:
            locked.add(str(col))
        elif getattr(cr, "status", "") in REVIEWED_STATUSES:
            locked.add(str(col))
    return locked


def _write_overlay(store, table: str, entry: VerdictEntry, *, actor: str) -> None:
    decided_by = f"{STEWARD_VERDICT_ACTOR_PREFIX}{actor or 'cli'}"
    store.pii_decisions.set(
        table,
        PiiDecision(
            column=entry.column,
            status=entry.status,
            entity_type=entry.entity_type,
            payload=_payload_for(entry),
            decided_by=decided_by,
            run_id=entry.source_run_id,
            fingerprint_key=entry.fingerprint_key,
            name_normalized=entry.name_normalized,
            logical_type=entry.logical_type,
            physical_type=entry.physical_type,
            format_signature=entry.format_signature,
            evidence_digest=entry.evidence_digest,
            lifecycle_state="active",
            decision_version=int(entry.decision_version or 1),
            reason=entry.reason or "steward-verdict-path",
        ),
    )


def _definition_patch(value: Any) -> Optional[dict]:
    if isinstance(value, str) and value.strip():
        return {"description": value}
    if not isinstance(value, dict):
        return None
    patch: dict[str, Any] = {}
    if value.get("description") is not None:
        patch["description"] = value["description"]
    elif value.get("text"):
        patch["description"] = value["text"]
    if value.get("businessName") is not None:
        patch["businessName"] = value["businessName"]
    if value.get("business") is not None:
        patch["business"] = value["business"]
    if value.get("tags") is not None:
        patch["tags"] = list(value["tags"])
    return patch or None


def attach_steward_verdict_path(
    store,
    table: str,
    path: Path | str,
    *,
    actor: str = "cli",
    schema_columns: Optional[Iterable[str]] = None,
    apply_now: bool = True,
) -> AttachReport:
    """Load *path* and lock matching human-verified columns on *table*.

    Overlay rows are always written. When ``apply_now`` and an active contract
    exist, overlays are re-applied in one upsert so the contract matches.
    """
    table = str(table or "").strip()
    if not table:
        raise StewardAttachError("table is required to attach steward verdicts")
    report = AttachReport(table=table, path=str(path))
    loaded = load_steward_verdict_path(path)
    schema = _schema_names(store, table, schema_columns)
    fingerprints = _fingerprints_from_store(store, table)
    seen: set[str] = set()
    def_patches: dict[str, dict] = {}

    for file, package, raw in loaded:
        report.files_loaded.append(str(file))
        for entry in package.entries:
            if not _verdict_table_matches(entry, package, raw, table):
                if entry.column:
                    report.skipped_other_table.append(entry.column)
                continue
            if not entry.column:
                report.skipped_pending.append("")
                continue
            if schema and entry.column not in schema:
                report.skipped_schema.append(entry.column)
                continue
            if (
                entry.fingerprint_key
                and fingerprints.get(entry.column)
                and entry.fingerprint_key != fingerprints[entry.column]
            ):
                report.skipped_stale.append(entry.column)
                continue
            if entry.column in seen:
                continue
            seen.add(entry.column)
            _write_overlay(store, table, entry, actor=actor)
            report.applied.append(entry.column)

        if raw:
            for row in raw.get("entries") or []:
                if not isinstance(row, dict):
                    continue
                decision = str(row.get("decision") or "")
                column = row.get("column")
                field = row.get("field")
                row_table = str(row.get("table") or raw.get("table") or "")
                if row_table and row_table != table:
                    continue
                if field == "pii" and decision in ("needs_review", "reject"):
                    if column and column not in report.skipped_needs_review:
                        report.skipped_needs_review.append(str(column))
                    continue
                if field != "definition" or decision not in REVIEWED_DECISIONS or not column:
                    continue
                if schema and column not in schema:
                    continue
                patch = _definition_patch(row.get("value"))
                if patch:
                    def_patches[str(column)] = patch

    patched_defs = False
    if def_patches and store.get_active(table) is not None:
        try:
            store.patch_definitions(
                table,
                column_patches=def_patches,
                decided_by=f"{STEWARD_VERDICT_ACTOR_PREFIX}{actor or 'cli'}",
                run_id="steward-verdict-path",
            )
            report.definitions_applied = sorted(def_patches)
            patched_defs = True
            report.overlay_applied = True
        except Exception:
            patched_defs = False

    if apply_now and report.applied and not patched_defs and store.get_active(table) is not None:
        store._reapply_overlays(
            table,
            workflow="steward-verdict-attach",
            run_id="steward-verdict-path",
        )
        report.overlay_applied = True
    return report
