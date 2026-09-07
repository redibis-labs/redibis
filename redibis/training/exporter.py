"""TrainingDatasetExporter — harvest overlays ⨝ telemetry into a training corpus."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from redibis.contracts.privacy import col_entity_type, col_privacy_classification, iter_columns
from redibis.store.contract_store import ContractStore
from redibis.store.pii_decisions import infer_engine_baseline_from_telemetry
from redibis.store.review_store import ReviewStore
from redibis.training.dataset import TrainingDataset, TrainingExample, context_hash
from redibis.training.features import build_column_features
from redibis.training.portable_assert import PortableLeakError, assert_portable_row

PathLike = Union[str, Path]


@dataclass
class ExportOptions:
    """Filters and residency controls for a training export."""

    tables: Optional[Sequence[str]] = None
    database: Optional[str] = None
    label_sources: Optional[Sequence[str]] = None  # human_decision | review | contract_confirmed
    min_confidence: Optional[float] = None
    include_samples: bool = False
    samples_by_column: Mapping[str, Sequence[str]] = field(default_factory=dict)
    # key: "table.column" → sample strings
    max_samples_per_column: int = 5
    table_domain: str = ""
    disagreements_only: bool = False


def _props_by_name(contract: Optional[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not contract:
        return out
    for prop in iter_columns(contract):
        name = str(prop.get("name") or "")
        if name:
            out[name] = prop
    return out


def _engine_said_pii(telemetry: dict[str, Any]) -> Optional[bool]:
    """Best-effort engine baseline from column telemetry (join-derived fallback)."""
    return infer_engine_baseline_from_telemetry(telemetry).get("engine_is_pii")


def _classification_from_sources(
    prop: dict,
    decision: dict,
    review_approved: Optional[dict],
) -> str:
    payload = decision.get("payload") or {}
    if isinstance(payload, dict):
        cls = col_privacy_classification(payload) or str(payload.get("classification") or "")
        if cls:
            return cls
    if review_approved:
        cls = str(review_approved.get("classification") or "")
        if cls:
            return cls
    return col_privacy_classification(prop) or str(prop.get("classification") or "")


def _resolve_corrected_engine(
    decision: dict,
    telemetry: dict[str, Any],
    *,
    is_pii: bool,
) -> tuple[Optional[bool], bool, Optional[bool]]:
    """Return ``(corrected_engine, derived, engine_is_pii)``.

    Prefers the stored baseline on the PII decision (Phase 1b). Falls back to
    join-derived telemetry when the stored field is absent (legacy overlays).
    """
    if "engine_is_pii" in decision and decision.get("engine_is_pii") is not None:
        engine_pii = bool(decision["engine_is_pii"])
        return (engine_pii != bool(is_pii), False, engine_pii)
    if "engine_is_pii" in decision and decision.get("engine_is_pii") is None:
        # Explicitly captured as unknown at decision time.
        if any(
            k in decision
            for k in ("engine_confidence", "engine_entity_type", "engine_decision_rule")
        ):
            return (None, False, None)
    engine_pii = _engine_said_pii(telemetry)
    if engine_pii is None:
        return (None, True, None)
    return (bool(engine_pii) != bool(is_pii), True, engine_pii)


class TrainingDatasetExporter:
    """Join PiiDecisionStore (+ ReviewStore / contract) with column telemetry."""

    def __init__(self, store: ContractStore, *, review_store: Optional[ReviewStore] = None):
        self.store = store
        self.review_store = review_store or ReviewStore(store.backend, store.bucket)

    def export(self, options: Optional[ExportOptions] = None) -> TrainingDataset:
        opts = options or ExportOptions()
        if opts.include_samples:
            residency = "local"
        else:
            residency = "portable"

        tables = list(opts.tables) if opts.tables else list(self.store.list_tables())
        if opts.database:
            prefix = opts.database.rstrip(".") + "."
            tables = [t for t in tables if t == opts.database or t.startswith(prefix)]

        allowed_sources = None
        if opts.label_sources:
            allowed_sources = {str(s).lower() for s in opts.label_sources}

        examples: list[TrainingExample] = []
        seen_hashes: set[str] = set()
        audit = {
            "tables_scanned": 0,
            "decisions_seen": 0,
            "rows_emitted": 0,
            "rows_skipped_filter": 0,
            "rows_deduped": 0,
            "portable_assert_ok": 0,
            "stored_corrected_engine": 0,
            "derived_corrected_engine": 0,
            "unknown_engine_baseline": 0,
        }

        for table in tables:
            audit["tables_scanned"] += 1
            active = self.store.get_active(table) or {}
            props = _props_by_name(active)
            decisions = self.store.get_pii_decisions(table) or {}
            telemetry = self.store.metadata.get_column_telemetry(table) or {}
            try:
                review_state = self.review_store.get(table)
                reviews = dict(review_state.columns or {})
            except Exception:
                reviews = {}

            # Primary: columns with a human PII decision.
            columns = set(decisions.keys())
            # Also include approved/edited reviews without an overlay (contract_confirmed path).
            for col, rev in reviews.items():
                status = getattr(rev, "status", None) or (rev.get("status") if isinstance(rev, dict) else "")
                if status in ("approved", "edited"):
                    columns.add(col)

            for column in sorted(columns):
                decision = decisions.get(column) or {}
                tel = telemetry.get(column) or {}
                prop = props.get(column) or {"name": column}
                rev = reviews.get(column)
                rev_status = ""
                rev_approved: Optional[dict] = None
                rev_by = ""
                rev_at = ""
                if rev is not None:
                    if hasattr(rev, "status"):
                        rev_status = str(rev.status or "")
                        rev_approved = dict(rev.approved or {})
                        rev_by = str(rev.reviewed_by or "")
                        rev_at = str(rev.reviewed_at or "")
                    elif isinstance(rev, dict):
                        rev_status = str(rev.get("status") or "")
                        rev_approved = dict(rev.get("approved") or {})
                        rev_by = str(rev.get("reviewed_by") or "")
                        rev_at = str(rev.get("reviewed_at") or "")

                if decision:
                    audit["decisions_seen"] += 1
                    status = str(decision.get("status") or "")
                    is_pii = status == "pii"
                    source = "human_decision"
                    entity = str(decision.get("entity_type") or "") or col_entity_type(
                        decision.get("payload") or {}
                    )
                    decided_by = str(decision.get("decided_by") or "")
                    decided_at = str(decision.get("ts") or "")
                elif rev_status in ("approved", "edited"):
                    # Review-only gold (no overlay yet).
                    flag = (rev_approved or {}).get("pii_flag")
                    if flag is None:
                        # Infer from approved snapshot classification / tags.
                        tags = rev_approved.get("tags") or [] if rev_approved else []
                        cls = str((rev_approved or {}).get("classification") or "")
                        is_pii = bool(cls.startswith("pii") or "pii" in {str(t).lower() for t in tags})
                    else:
                        is_pii = bool(flag)
                    source = "review"
                    entity = str((rev_approved or {}).get("entity_type") or "") or col_entity_type(prop)
                    decided_by = rev_by
                    decided_at = rev_at
                else:
                    audit["rows_skipped_filter"] += 1
                    continue

                if allowed_sources and source not in allowed_sources:
                    audit["rows_skipped_filter"] += 1
                    continue

                conf = tel.get("confidence")
                try:
                    conf_f = float(conf) if conf is not None else None
                except (TypeError, ValueError):
                    conf_f = None
                if opts.min_confidence is not None and conf_f is not None:
                    if conf_f < float(opts.min_confidence):
                        audit["rows_skipped_filter"] += 1
                        continue

                engine_pii: Optional[bool]
                corrected: Optional[bool]
                derived: bool
                if decision:
                    corrected, derived, engine_pii = _resolve_corrected_engine(
                        decision,
                        tel if isinstance(tel, dict) else {},
                        is_pii=bool(is_pii),
                    )
                else:
                    engine_pii = _engine_said_pii(tel if isinstance(tel, dict) else {})
                    if engine_pii is None:
                        corrected, derived = None, True
                    else:
                        corrected = bool(engine_pii) != bool(is_pii)
                        derived = True

                if corrected is None:
                    audit["unknown_engine_baseline"] += 1
                elif derived:
                    if corrected:
                        audit["derived_corrected_engine"] += 1
                else:
                    if corrected:
                        audit["stored_corrected_engine"] += 1

                if opts.disagreements_only and corrected is not True:
                    audit["rows_skipped_filter"] += 1
                    continue

                classification = _classification_from_sources(prop, decision, rev_approved)
                features = build_column_features(
                    table=table,
                    column=column,
                    prop=prop,
                    telemetry=tel if isinstance(tel, dict) else {},
                    table_domain=opts.table_domain,
                )
                ch = context_hash(
                    table=table,
                    column=column,
                    parts=[
                        is_pii,
                        entity,
                        classification,
                        features.get("logical_type", ""),
                        features.get("engine_confidence", ""),
                        source,
                    ],
                )
                if ch in seen_hashes:
                    audit["rows_deduped"] += 1
                    continue
                seen_hashes.add(ch)

                label: dict[str, Any] = {
                    "is_pii": bool(is_pii),
                    "entity_type": entity or None,
                    "classification": classification or None,
                    "source": source,
                    "corrected_engine": corrected,
                    "corrected_engine_derived": derived,
                }

                provenance = {
                    "table": table,
                    "column": column,
                    "decided_by": decided_by,
                    "decided_at": decided_at,
                    "context_hash": ch,
                    "engine_baseline": engine_pii,
                }
                if decision.get("run_id"):
                    provenance["run_id"] = decision.get("run_id")
                if decision.get("engine_confidence") is not None:
                    provenance["engine_confidence"] = decision.get("engine_confidence")
                if decision.get("engine_entity_type"):
                    provenance["engine_entity_type"] = decision.get("engine_entity_type")
                if decision.get("engine_decision_rule"):
                    provenance["engine_decision_rule"] = decision.get("engine_decision_rule")

                samples = None
                text_context = None
                contains_raw = False
                if opts.include_samples:
                    key = f"{table}.{column}"
                    raw = opts.samples_by_column.get(key) or opts.samples_by_column.get(column)
                    if raw:
                        samples = [str(v) for v in list(raw)[: int(opts.max_samples_per_column)]]
                        contains_raw = True
                    # Catalog/contract description only when include_samples (local track).
                    desc = prop.get("description") or (prop.get("business") or {}).get("description")
                    if desc:
                        text_context = str(desc)
                        contains_raw = True

                example = TrainingExample(
                    features=features,
                    label=label,
                    provenance=provenance,
                    residency=residency,  # type: ignore[arg-type]
                    samples=samples,
                    text_context=text_context,
                    contains_raw_values=contains_raw,
                )
                row = example.to_dict()
                if residency == "portable":
                    assert_portable_row(row, path=f"{table}.{column}")
                    audit["portable_assert_ok"] += 1
                examples.append(example)
                audit["rows_emitted"] += 1

        ds = TrainingDataset(
            examples=examples,
            residency=residency,  # type: ignore[arg-type]
            source="training_export",
            meta={
                "audit": audit,
                "include_samples": bool(opts.include_samples),
                "filters": {
                    "tables": list(opts.tables) if opts.tables else None,
                    "database": opts.database,
                    "label_sources": list(opts.label_sources) if opts.label_sources else None,
                    "min_confidence": opts.min_confidence,
                    "disagreements_only": opts.disagreements_only,
                },
            },
        )
        return ds

    def export_to_path(
        self,
        path: PathLike,
        options: Optional[ExportOptions] = None,
    ) -> TrainingDataset:
        ds = self.export(options)
        if ds.residency == "portable":
            for i, ex in enumerate(ds.examples):
                try:
                    assert_portable_row(ex.to_dict(), path=f"row[{i}]")
                except PortableLeakError:
                    raise
        ds.write_jsonl(path)
        return ds
