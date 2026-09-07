"""
redibis.services.review_service
===============================
Final-Review business logic — assemble a per-column review payload for a contract, apply the
reviewer's approve/edit/reject actions through the EXISTING contract writers, checkpoint the
approval (resumable) in the ReviewStore, and write approved columns to the vector DB (memory
loop) so the agentic workflow can find similar columns.

Reuse map (nothing reinvented):
  - read columns / tags / classification / definition / glossary : ContractStore.get_active
  - per-column telemetry (confidence, entity, profiling)          : ContractStore.get_metadata
  - edit tags / classification                                    : ContractStore.patch_column_privacy
  - edit definition / glossary                                    : ContractStore.patch_definitions
  - approve / override PII flag                                   : ContractStore.set_pii_decision
  - approved column -> vector DB (similarity search)              : memory.writer.record_review
  - resumable checkpoint + fully-approved flag                    : ReviewStore (_meta/reviews/{table}.json)
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from redibis.contracts.privacy import column_is_pii, col_entity_type
from redibis.store.contract_store import ContractStore
from redibis.store.review_store import ColumnReview, ReviewStore


class ReviewInputError(ValueError):
    """Invalid review request (missing reviewer, bad payload)."""


def _iter_props(active: dict) -> Iterator[tuple[str, dict]]:
    """Yield (column_name, property_dict) from an ODCS contract (schema[].properties[] or flat)."""
    schema = active.get("schema") or active.get("properties") or []
    if not isinstance(schema, list):
        return
    for obj in schema:
        if not isinstance(obj, dict):
            continue
        props = obj.get("properties")
        if isinstance(props, list):
            for p in props:
                if isinstance(p, dict) and p.get("name"):
                    yield str(p["name"]), p
        elif obj.get("name"):
            yield str(obj["name"]), obj


def _has_pii_tag(tags: list) -> bool:
    return any(str(t).lower() in ("pii", "sensitive", "personal") for t in (tags or []))


def _require_reviewer(reviewer: str) -> str:
    name = (reviewer or "").strip()
    if not name:
        raise ReviewInputError("reviewer is required — provide who performed this review action")
    return name


def _pending_review() -> dict:
    return {
        "status": "pending",
        "reviewed_by": "",
        "reviewed_at": "",
        "approved": {},
        "note": "",
    }


def _col_definition(prop: dict) -> str:
    """Business definition from active contract (incl. definitions-view / business block)."""
    business = prop.get("business")
    if isinstance(business, dict):
        bd = business.get("definition")
        if bd not in (None, ""):
            return str(bd)
    for key in ("description", "businessName"):
        val = prop.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _pii_flag(prop: dict, pii_decisions: dict, column: str) -> bool:
    dec = (pii_decisions or {}).get(column) or {}
    if dec.get("status") == "not_pii":
        return False
    if dec.get("status") == "pii":
        return True
    return column_is_pii(prop)


def _norm_glossary(glossary: list) -> list:
    out = []
    for g in glossary or []:
        if isinstance(g, str):
            out.append({"term": g, "uri": ""})
        elif isinstance(g, dict):
            out.append({
                "term": str(g.get("term") or g.get("name") or ""),
                "uri": str(g.get("uri") or g.get("url") or ""),
            })
    return out


def _snapshots_match(current: dict, approved: dict) -> bool:
    """True when an approval snapshot still matches the active contract column."""
    if not approved:
        return True
    cur_tags = sorted(str(t) for t in (current.get("tags") or []))
    app_tags = sorted(str(t) for t in (approved.get("tags") or []))
    return (
        bool(current.get("pii_flag")) == bool(approved.get("pii_flag"))
        and cur_tags == app_tags
        and str(current.get("classification") or "") == str(approved.get("classification") or "")
        and str(current.get("definition") or "") == str(approved.get("definition") or "")
        and _norm_glossary(current.get("glossary") or []) == _norm_glossary(approved.get("glossary") or [])
    )


class ReviewService:
    """Final-Review orchestration. REST routes are thin adapters over this."""

    def __init__(self, store: ContractStore, review_store: Optional[ReviewStore] = None):
        self.store = store
        self.reviews = review_store or ReviewStore(store.backend, store.bucket)

    # ── read model ────────────────────────────────────────────────────────────
    def get_review(self, table: str) -> dict:
        active = self.store.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        meta = {}
        try:
            meta = self.store.get_metadata(table) or {}
        except Exception:
            meta = {}
        col_meta = meta.get("columns") if isinstance(meta.get("columns"), dict) else {}
        pii_decisions = self.store.get_pii_decisions(table)
        state = self.reviews.get(table)
        props = list(_iter_props(active))
        items = []
        for name, prop in props:
            item = self._build_item(name, prop, col_meta, state, pii_decisions)
            review = state.columns.get(name)
            if review and review.status == "approved":
                current_snap = self._snapshot(prop, pii_decisions)
                if not _snapshots_match(current_snap, review.approved):
                    state = self.reviews.reset_column(table, name)
                    try:
                        self.store.metadata.clear_final_review_column(table, name)
                    except Exception:
                        pass
                    item["review"] = _pending_review()
            items.append(item)
        state = self.reviews.get(table)
        return {
            "table": table,
            "contract_uuid": str(active.get("contract_uuid") or ""),
            "columns": items,
            "approved_count": state.approved_count,
            "total_columns": len(props),
            "fully_approved": state.fully_approved,
            "updated_at": state.updated_at,
            "updated_by": state.updated_by,
        }

    def status(self, table: str) -> dict:
        active = self.store.get_active(table)
        total = len(list(_iter_props(active))) if active else 0
        state = self.reviews.get(table)
        return {
            "table": table,
            "approved_count": state.approved_count,
            "total_columns": total,
            "fully_approved": state.fully_approved and total > 0 and state.approved_count >= total,
            "updated_at": state.updated_at,
        }

    def _build_item(self, name: str, prop: dict, col_meta: dict, state, pii_decisions: dict) -> dict:
        privacy = prop.get("privacy") if isinstance(prop.get("privacy"), dict) else {}
        tags = prop.get("tags") or []
        classification = str(privacy.get("classification") or prop.get("classification") or "")
        definition = _col_definition(prop)
        glossary = prop.get("authoritativeDefinitions") or prop.get("glossary") or []
        logical = str(prop.get("logicalType") or prop.get("physicalType") or "string")
        cm = col_meta.get(name) if isinstance(col_meta.get(name), dict) else {}
        pii_flag = _pii_flag(prop, pii_decisions, name)
        entity = col_entity_type(prop) or str(cm.get("entity_type") or prop.get("entityType") or "")
        review = state.columns.get(name)
        return {
            "column": name,
            "logical_type": logical,
            "pii": {
                "flag": pii_flag,
                "entity_type": entity,
                "confidence": cm.get("confidence"),
            },
            "definition": definition,
            "glossary": glossary,
            "tags": list(tags),
            "classification": classification,
            "profiling": cm.get("profiling") or cm.get("profiling_features") or [],
            "review": review.to_dict() if review else _pending_review(),
        }

    # ── actions (delegate to existing writers) ──────────────────────────────────
    def edit_column(
        self,
        table: str,
        column: str,
        *,
        tags: Optional[list] = None,
        classification: Optional[str] = None,
        definition: Optional[str] = None,
        glossary: Optional[list] = None,
        pii_status: Optional[str] = None,   # "pii" | "not_pii"
        entity_type: Optional[str] = None,  # entity correction, only with pii_status="pii"
        reason: str = "",
        reviewer: str = "",
        run_id: str = "",
        evidence_col_block: Optional[dict] = None,
    ) -> dict:
        who = _require_reviewer(reviewer)
        if tags is not None or classification is not None:
            self.store.patch_column_privacy(
                table, column, classification=classification, tags=tags, decided_by=who,
            )
        if definition is not None or glossary is not None:
            patch: dict[str, Any] = {}
            if definition is not None:
                patch["description"] = definition
            if glossary is not None:
                patch["authoritativeDefinitions"] = glossary
            self.store.patch_definitions(table, column_patches={column: patch}, decided_by=who)
        if pii_status in ("pii", "not_pii"):
            from redibis.review.fingerprint import (
                evidence_digest,
                fingerprint_metadata_from_evidence,
                fingerprint_metadata_from_prop,
            )

            digest = ""
            if evidence_col_block:
                # Fingerprint from the *reviewed run's* evidence (real profile,
                # so ``format_signature`` reflects observed data) rather than the
                # contract property alone (whose profile is empty and always
                # yields ``format_signature="unknown"`` — see plan §5: "every
                # decision captures the selected run's evidence digest and
                # format fingerprint"). A decision fingerprinted this way
                # compares correctly against future evidence-derived drift
                # checks instead of always mismatching.
                fp_meta = fingerprint_metadata_from_evidence(column, evidence_col_block)
                digest = evidence_digest(evidence_col_block)
            else:
                active = self.store.get_active(table)
                prop = self._prop(active, column)
                fp_meta = fingerprint_metadata_from_prop(column, prop)
            existing = (self.store.get_pii_decisions(table) or {}).get(column) or {}
            version = int(existing.get("decision_version") or 0) + 1
            payload = {"entity_type": entity_type} if pii_status == "pii" and entity_type else None
            self.store.set_pii_decision(
                table,
                column,
                pii_status,
                entity_type=entity_type if pii_status == "pii" else None,
                payload=payload,
                decided_by=who,
                run_id=run_id,
                evidence_digest=digest,
                reason=reason,
                lifecycle_state="active",
                decision_version=version,
                **fp_meta,
            )
        self._checkpoint(table, column, status="edited", reviewer=who)
        return self.get_review(table)

    def approve_column(
        self,
        table: str,
        column: str,
        *,
        approved: Optional[dict] = None,
        reviewer: str = "",
        note: str = "",
    ) -> dict:
        who = _require_reviewer(reviewer)
        active = self.store.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        prop = self._prop(active, column)
        snap = approved or self._snapshot(prop, self.store.get_pii_decisions(table))
        self._record_to_memory(table, column, prop, snap, who)
        self._checkpoint(table, column, status="approved", reviewer=who,
                         approved=snap, note=note)
        return self.get_review(table)

    def reject_column(self, table: str, column: str, *, reviewer: str = "", note: str = "") -> dict:
        who = _require_reviewer(reviewer)
        self._checkpoint(table, column, status="rejected", reviewer=who, note=note)
        return self.get_review(table)

    def reset_column(self, table: str, column: str) -> dict:
        self.reviews.reset_column(table, column)
        try:
            self.store.metadata.clear_final_review_column(table, column)
        except Exception:
            pass
        return self.get_review(table)

    def finalize(self, table: str, *, reviewer: str = "") -> dict:
        who = _require_reviewer(reviewer)
        state = self.reviews.finalize(table, updated_by=who)
        try:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat()
            self.store.metadata.patch_final_review_summary(table, {
                "fully_approved": state.fully_approved,
                "finalized_by": who,
                "finalized_at": ts,
                "contract_uuid": state.contract_uuid,
                "approved_count": state.approved_count,
                "total_columns": state.total_columns,
                "review_status": "fully_approved" if state.fully_approved else "in_progress",
            })
        except Exception:
            pass
        return self.get_review(table)

    # ── helpers ─────────────────────────────────────────────────────────────────
    def _prop(self, active: dict, column: str) -> dict:
        for name, prop in _iter_props(active):
            if name == column:
                return prop
        raise ValueError(f"Column {column!r} not in {active.get('table') or 'contract'}")

    def _snapshot(self, prop: dict, pii_decisions: Optional[dict] = None) -> dict:
        privacy = prop.get("privacy") if isinstance(prop.get("privacy"), dict) else {}
        col = str(prop.get("name") or "")
        return {
            "pii_flag": _pii_flag(prop, pii_decisions or {}, col),
            "tags": list(prop.get("tags") or []),
            "classification": str(privacy.get("classification") or prop.get("classification") or ""),
            "definition": _col_definition(prop),
            "glossary": prop.get("authoritativeDefinitions") or prop.get("glossary") or [],
        }

    def _checkpoint(self, table, column, *, status, reviewer, approved=None, note=""):
        active = self.store.get_active(table)
        total = len(list(_iter_props(active))) if active else 0
        cr = ColumnReview(
            column=column, status=status, approved=approved or {},
            reviewed_by=reviewer, note=note,
        )
        state = self.reviews.set_column(
            table, cr, total_columns=total,
            contract_uuid=str((active or {}).get("contract_uuid") or ""),
            updated_by=reviewer,
        )
        saved = state.columns.get(column)
        if saved and saved.reviewed_by:
            try:
                self.store.metadata.record_final_review_column(
                    table,
                    column,
                    status=saved.status,
                    reviewed_by=saved.reviewed_by,
                    reviewed_at=saved.reviewed_at,
                    note=saved.note,
                    approved=saved.approved,
                )
            except Exception:
                pass

    def _record_to_memory(self, table, column, prop, approved, reviewer):
        """Write the approved column's format-signature fingerprint to the vector DB (best-effort)."""
        try:
            from redibis.memory.decision import ReviewDecision
            from redibis.memory.writer import fingerprint_from_column_prop, record_review
            mc = getattr(self.store, "memory_config", None)
            if mc is None or not getattr(mc, "enabled", False):
                return
            domain = getattr(mc, "domain", "") or ""
            fp = fingerprint_from_column_prop(table, column, prop, domain=domain)
            privacy = prop.get("privacy") if isinstance(prop.get("privacy"), dict) else {}
            decision = ReviewDecision(
                fingerprint_key="",
                table=table,
                column=column,
                pii_verdict="pii" if (approved.get("pii_flag") or _has_pii_tag(prop.get("tags") or [])) else "not_pii",
                classification=str(approved.get("classification") or privacy.get("classification") or "") or None,
                business_definition=str(approved.get("definition") or prop.get("description") or "") or None,
                reviewer=reviewer,
                rationale="final-review approval",
            )
            record_review(
                memory_config=mc,
                fingerprint=fp,
                decision=decision,
                memory_store=getattr(self.store, "_memory_store", None),
            )
        except Exception:
            # memory is an enrichment, never block the approval checkpoint
            pass
