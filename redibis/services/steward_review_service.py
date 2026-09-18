"""Steward Review read model — composes existing writers; does not replace them.

Layout: table → overview stats → one column page. Writes go through
ReviewService / PiiDecisionStore / DefinitionDecisionStore / QualityDecisionStore /
FieldDecisionStore. Finalize stamps A0–A5 under one review_digest.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.contracts.privacy import col_entity_type, column_is_pii
from redibis.review.rationale import RationaleError, codes_for_choice, validate_rationale
from redibis.services.evidence_review_service import EvidenceReviewService
from redibis.services.review_service import ReviewInputError, ReviewService, _iter_props
from redibis.store.contract_store import ContractStore
from redibis.store.field_decisions import FieldDecision, fingerprint_kwargs_from_prop
from redibis.store.generation_ledger import FIELDS, Generation, GenerationLedger
from redibis.store.profile_store import ProfileStore
from redibis.store.review_store import (
    BLOCKING_DECISIONS,
    DECISION_TO_STATUS,
    FieldVerdict,
    REVIEWED_DECISIONS,
    REVIEWED_STATUSES,
    ReviewStore,
    VALID_DECISIONS,
)

TABLE_ITEMS = ("name", "description", "owner")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pii_value(gen: Generation) -> Optional[bool]:
    val = gen.value
    if isinstance(val, dict):
        if "is_pii" in val:
            return bool(val["is_pii"])
        if "detected" in val:
            return bool(val["detected"])
    if isinstance(val, bool):
        return val
    return None


AGREEMENT = ("no_evidence", "unanimous", "majority", "contested")


def _agreement(gens: list[Generation]) -> str:
    votes = []
    for g in gens:
        if g.source in ("human", "supplied"):
            continue
        v = _pii_value(g)
        if v is not None:
            votes.append(v)
    if not votes:
        return "no_evidence"
    if len(set(votes)) == 1:
        return "unanimous"
    yes = sum(1 for v in votes if v)
    no = len(votes) - yes
    if yes == no:
        return "contested"
    return "majority" if max(yes, no) > 1 else "contested"


def _table_quality_item_ids(active: dict) -> list[str]:
    ids: list[str] = []
    from redibis.contracts.rules import stable_rule_id
    for schema_obj in active.get("schema") or []:
        for q in schema_obj.get("quality") or []:
            if isinstance(q, dict):
                ids.append(f"quality:{stable_rule_id(None, q)}")
    return ids


def _table_item_keys(active: dict) -> list[str]:
    return list(TABLE_ITEMS) + _table_quality_item_ids(active)


def _schema0(active: dict) -> dict:
    schema = active.get("schema") or []
    return schema[0] if schema and isinstance(schema[0], dict) else {}


class StewardReviewService:
    def __init__(
        self,
        store: ContractStore,
        review: Optional[ReviewService] = None,
        evidence: Optional[EvidenceReviewService] = None,
        ledger: Optional[GenerationLedger] = None,
        profiles: Optional[ProfileStore] = None,
    ):
        self.store = store
        self.reviews = ReviewStore(store.backend, store.bucket)
        self.review = review or ReviewService(store, self.reviews)
        self.evidence = evidence or EvidenceReviewService(
            store, review_store=self.reviews, pii_store=store.pii_decisions,
        )
        self.ledger = ledger or getattr(store, "generation_ledger", None) or GenerationLedger(
            store.backend, store.bucket,
        )
        self.profiles = profiles or getattr(store, "profiles", None) or ProfileStore(
            store.backend, store.bucket, consent=store.sampling_consent,
        )

    def overview(self, table: str) -> dict:
        active = self.store.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        props = list(_iter_props(active))
        state = self.reviews.get(table)
        schema = _schema0(active)
        pii_decisions = self.store.get_pii_decisions(table)
        index = []
        pii_by_entity: dict[str, int] = {}
        class_dist: dict[str, int] = {}
        agreement_counts = {"no_evidence": 0, "unanimous": 0, "majority": 0, "contested": 0}
        human_of_engine = 0
        human_of_synth = 0
        quality_total = 0
        quality_fail = 0
        quality_pass = 0
        nulls: list[float] = []
        ndvs: list[float] = []
        samples_cols = 0
        run_id = self.profiles.latest_run_id(table)

        for i, (name, prop) in enumerate(props):
            cr = state.columns.get(name)
            status = cr.status if cr else "pending"
            privacy = prop.get("privacy") if isinstance(prop.get("privacy"), dict) else {}
            classification = str(privacy.get("classification") or prop.get("classification") or "")
            if classification:
                class_dist[classification] = class_dist.get(classification, 0) + 1
            pii = column_is_pii(prop)
            dec = pii_decisions.get(name) or {}
            if dec.get("status") == "not_pii":
                pii = False
            elif dec.get("status") == "pii":
                pii = True
            entity = col_entity_type(prop) or str(dec.get("entity_type") or "")
            if pii:
                pii_by_entity[entity or "unknown"] = pii_by_entity.get(entity or "unknown", 0) + 1
            latest = self.ledger.latest_by_source(table, name, "pii")
            agr = _agreement(list(latest.values()))
            agreement_counts[agr] = agreement_counts.get(agr, 0) + 1
            human = latest.get("human")
            if human:
                if human.detail.get("overrode") == "llm_synthesis" or latest.get("llm_synthesis"):
                    if _pii_value(human) != _pii_value(latest.get("llm_synthesis") or human):
                        human_of_synth += 1
                engine_srcs = [latest[s] for s in ("regex", "ner", "llm") if s in latest]
                if engine_srcs and any(_pii_value(e) != _pii_value(human) for e in engine_srcs):
                    human_of_engine += 1
            for q in prop.get("quality") or []:
                if isinstance(q, dict):
                    quality_total += 1
            if run_id:
                try:
                    payload = self.profiles.read(table, run_id, name, include_samples=False)
                    stats = payload.get("stats") or {}
                    nr = stats.get("null_rate")
                    if nr is not None:
                        nulls.append(float(nr))
                    ndv = stats.get("ndv_ratio") or stats.get("ndv")
                    if ndv is not None:
                        ndvs.append(float(ndv))
                    if self.profiles.has_samples(table, run_id, name):
                        samples_cols += 1
                    for q in payload.get("quality") or []:
                        if isinstance(q, dict):
                            quality_total += 1
                            if q.get("passed") is False or q.get("success") is False:
                                quality_fail += 1
                            elif q.get("passed") is True or q.get("success") is True:
                                quality_pass += 1
                except Exception:
                    pass
            index.append({
                "column": name,
                "position": i + 1,
                "status": status,
                "agreement": agr,
                "pii": pii,
                "entity_type": entity,
                "classification": classification,
            })

        required_table = _table_item_keys(active)
        blockers = self.reviews.guarantee_blockers(
            table,
            required_columns=[n for n, _ in props],
            required_table_items=required_table,
        )
        reviewed = sum(1 for c in index if c["status"] in REVIEWED_STATUSES)
        needs = sum(1 for c in index if c["status"] == "needs_review")
        rejected = sum(1 for c in index if c["status"] == "rejected")
        pending = sum(1 for c in index if c["status"] == "pending")
        team = active.get("team") or schema.get("team") or []
        owner = ""
        if isinstance(team, list) and team:
            first = team[0]
            owner = first.get("name") or first.get("username") or str(first) if isinstance(first, dict) else str(first)
        elif isinstance(team, str):
            owner = team

        return {
            "table": table,
            "contract_uuid": str(active.get("contract_uuid") or ""),
            "table_section": {
                "name": schema.get("name") or active.get("name") or table,
                "description": schema.get("description") or active.get("description") or "",
                "owner": owner,
                "quality_rules": list(schema.get("quality") or []),
                "review": {
                    k: (state.table_review.items[k].to_dict()
                        if k in state.table_review.items else None)
                    for k in required_table
                },
            },
            "columns": index,
            "of": len(props),
            "stats": {
                "columns_total": len(props),
                "reviewed": reviewed,
                "needs_review": needs,
                "rejected": rejected,
                "pending": pending,
                "pii_columns": pii_by_entity,
                "classification": class_dist,
                "engine_agreement": agreement_counts,
                "human_overrides": {"of_engine": human_of_engine, "of_synthesis": human_of_synth},
                "quality": {
                    "rules_total": quality_total,
                    "failing": quality_fail,
                    "passing": quality_pass,
                },
                "profile": {
                    "null_rate_p50": statistics.median(nulls) if nulls else None,
                    "ndv_ratio_p50": statistics.median(ndvs) if ndvs else None,
                    "columns_with_samples": samples_cols,
                },
            },
            "guarantee": {
                "guaranteed": bool(state.guaranteed) and not any(blockers.values()),
                "fully_approved": bool(state.fully_approved),
                "blockers": blockers,
                "reviewed": reviewed,
                "total": len(props),
            },
            "rationale_codes": codes_for_choice(""),
            "updated_at": state.updated_at,
            "updated_by": state.updated_by,
        }

    def column(
        self,
        table: str,
        column: str,
        *,
        actor: str = "",
        role: str = "",
        include_samples: bool = False,
    ) -> dict:
        active = self.store.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        props = list(_iter_props(active))
        found = None
        position = 0
        for i, (name, prop) in enumerate(props):
            if name == column:
                found = prop
                position = i + 1
                break
        if found is None:
            raise ValueError(f"Column {column!r} not in {table}")
        from redibis.webapp.security import role_can
        allow_samples = role_can(role or "explorer", "view_samples")
        run_id = self.profiles.latest_run_id(table)
        profile = {"stats": {}, "quality": [], "format_signature": "",
                   "samples": None, "samples_withheld": "no profile for this run"}
        if run_id:
            profile = self.profiles.read(
                table, run_id, column,
                actor=actor, include_samples=include_samples, allow_samples=allow_samples,
            )
            if not profile.get("stats") and not profile.get("samples_withheld"):
                profile["samples_withheld"] = ""
            if not self.profiles.manifest(table, run_id):
                profile["samples_withheld"] = profile.get("samples_withheld") or "no profile for this run"

        gens_all = self.ledger.get(table, column)
        by_field: dict[str, list[dict]] = {f: [] for f in FIELDS}
        latest_pii = []
        for g in gens_all:
            by_field.setdefault(g.field, []).append(g.to_dict())
        latest_map = {f: self.ledger.latest_by_source(table, column, f) for f in FIELDS}
        latest_payload = {
            f: [g.to_dict() for g in srcs.values()] for f, srcs in latest_map.items()
        }
        agreement = {f: _agreement(list(latest_map.get(f, {}).values())) for f in FIELDS}

        privacy = found.get("privacy") if isinstance(found.get("privacy"), dict) else {}
        pii_decisions = self.store.get_pii_decisions(table)
        dec = pii_decisions.get(column) or {}
        pii_flag = column_is_pii(found)
        if dec.get("status") == "not_pii":
            pii_flag = False
        elif dec.get("status") == "pii":
            pii_flag = True
        state = self.reviews.get(table)
        cr = state.columns.get(column)
        drift = "active"
        if dec.get("lifecycle_state") == "stale":
            drift = "stale"
        field_dec = self.store.field_decisions.get_column(table, column)
        for fd in field_dec.values():
            if str(fd.get("lifecycle_state") or "") == "stale":
                drift = "stale"

        glossary_candidates = _glossary_candidates(found, column)
        return {
            "column": column,
            "logical_type": str(found.get("logicalType") or found.get("physicalType") or "string"),
            "position": position,
            "of": len(props),
            "current": {
                "pii": pii_flag,
                "entity_type": col_entity_type(found) or dec.get("entity_type"),
                "classification": str(privacy.get("classification") or found.get("classification") or ""),
                "definition": _definition(found),
                "tags": list(found.get("tags") or []),
                "quality_rules": list(found.get("quality") or []),
                "masking": (privacy.get("masking_policy") or found.get("maskingPolicy") or {}),
            },
            "generations": latest_payload,
            "generations_history": by_field,
            "agreement": agreement,
            "profile": profile,
            "glossary_candidates": glossary_candidates,
            "review": cr.to_dict() if cr else {
                "status": "pending", "verdicts": {}, "reviewed_by": "", "note": "",
            },
            "drift": drift,
            "rationale_codes": {
                src: codes_for_choice(src, agreement=agreement.get("pii") or "")
                for src in ("regex", "ner", "llm", "llm_synthesis", "human", "custom_rule")
            },
        }

    def decide(
        self,
        table: str,
        column: str,
        field: str,
        verdict: FieldVerdict | dict,
        *,
        actor: str,
    ) -> dict:
        who = (actor or "").strip()
        if not who:
            raise ReviewInputError("reviewer is required — provide who performed this review action")
        fv = verdict if isinstance(verdict, FieldVerdict) else FieldVerdict.from_dict(verdict)
        if fv.decision not in VALID_DECISIONS:
            raise ReviewInputError(f"decision must be one of {sorted(VALID_DECISIONS)}")
        try:
            fv.rationale_code = validate_rationale(fv.rationale_code, fv.rationale_text)
        except RationaleError as exc:
            raise ReviewInputError(str(exc)) from exc
        from redibis.contracts.privacy import scrub_pii_text
        fv.rationale_text = scrub_pii_text(fv.rationale_text or "")
        fv.by = who
        fv.at = fv.at or _utc_now_iso()
        fv.field = field or fv.field

        if fv.chosen_source and fv.chosen_source != "human":
            latest = self.ledger.latest_by_source(table, column, fv.field)
            chosen = latest.get(fv.chosen_source)
            if chosen is None:
                # also search history
                hist = [g for g in self.ledger.get(table, column, field=fv.field)
                        if g.source == fv.chosen_source]
                chosen = hist[-1] if hist else None
            if chosen is None:
                raise ReviewInputError(
                    f"chosen_source {fv.chosen_source!r} must exist in the ledger for field {fv.field!r}"
                )
            fv.chosen_run_id = fv.chosen_run_id or chosen.run_id
            fv.evidence_refs = list(fv.evidence_refs) or [chosen.id]
            if fv.value is None:
                fv.value = chosen.value

        status = DECISION_TO_STATUS[fv.decision]
        self._route_write(table, column, fv, who)
        self._append_human_generation(table, column, fv, who)
        self.review._checkpoint(
            table, column, status=status, reviewer=who,
            note=fv.rationale_text, verdicts={fv.field: fv},
        )
        return self.column(table, column, actor=who)

    def decide_table(self, table: str, item: str, verdict: FieldVerdict | dict, *, actor: str) -> dict:
        who = (actor or "").strip()
        if not who:
            raise ReviewInputError("reviewer is required — provide who performed this review action")
        fv = verdict if isinstance(verdict, FieldVerdict) else FieldVerdict.from_dict(verdict)
        if fv.decision not in VALID_DECISIONS:
            raise ReviewInputError(f"decision must be one of {sorted(VALID_DECISIONS)}")
        try:
            fv.rationale_code = validate_rationale(fv.rationale_code, fv.rationale_text)
        except RationaleError as exc:
            raise ReviewInputError(str(exc)) from exc
        from redibis.contracts.privacy import scrub_pii_text
        fv.rationale_text = scrub_pii_text(fv.rationale_text or "")
        fv.by = who
        fv.at = fv.at or _utc_now_iso()
        fv.field = item
        active = self.store.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        required = _table_item_keys(active)
        if fv.decision in REVIEWED_DECISIONS and fv.decision != "no_action":
            self._route_table_write(table, item, fv, who)
        self.reviews.set_table_item(
            table, item, fv, updated_by=who, required_table_items=required,
        )
        return self.overview(table)

    def finalize(self, table: str, *, actor: str) -> dict:
        who = (actor or "").strip()
        if not who:
            raise ReviewInputError("reviewer is required — provide who performed this review action")
        active = self.store.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        cols = [n for n, _ in _iter_props(active)]
        required_table = _table_item_keys(active)
        blockers = self.reviews.guarantee_blockers(
            table, required_columns=cols, required_table_items=required_table,
        )
        if any(blockers.values()):
            return {
                "ok": False,
                "guaranteed": False,
                "blockers": blockers,
                "message": _blocker_message(blockers),
            }
        state = self.reviews.finalize(
            table, updated_by=who,
            required_table_items=required_table,
            required_columns=cols,
        )
        from redibis.review.artifacts import write_steward_artifacts
        artifacts = write_steward_artifacts(self, table, actor=who, state=state)
        try:
            self.store.metadata.patch_final_review_summary(table, {
                "fully_approved": state.fully_approved,
                "guaranteed": state.guaranteed,
                "finalized_by": who,
                "finalized_at": _utc_now_iso(),
                "contract_uuid": state.contract_uuid,
                "approved_count": state.approved_count,
                "reviewed_count": state.reviewed_count,
                "total_columns": state.total_columns,
                "review_status": "guaranteed" if state.guaranteed else "in_progress",
                "review_digest": artifacts.get("review_digest"),
            })
        except Exception:
            pass
        return {
            "ok": True,
            "guaranteed": True,
            "blockers": blockers,
            "artifacts": artifacts,
            "review": self.review.finalize(table, reviewer=who),
        }

    def list_artifacts(self, table: str) -> dict:
        from redibis.review.artifacts import list_artifacts
        return list_artifacts(self.store, table)

    def get_artifact(self, table: str, name: str) -> tuple[bytes, str, str]:
        from redibis.review.artifacts import get_artifact
        return get_artifact(self.store, table, name)

    def _route_write(self, table: str, column: str, fv: FieldVerdict, who: str) -> None:
        if fv.decision in ("needs_review", "no_action", "reject"):
            if fv.decision == "reject":
                self.review.reject_column(table, column, reviewer=who, note=fv.rationale_text)
            return
        if fv.field == "pii":
            is_pii = _coerce_pii(fv.value)
            if fv.decision == "edit" or fv.decision == "accept":
                status = "pii" if is_pii else "not_pii"
                entity = None
                if isinstance(fv.value, dict):
                    entity = fv.value.get("entity_type")
                self.review.edit_column(
                    table, column,
                    pii_status=status,
                    entity_type=entity,
                    reason=fv.rationale_text or fv.rationale_code,
                    reviewer=who,
                    run_id=fv.chosen_run_id,
                )
        elif fv.field in ("definition", "tags"):
            patch: dict[str, Any] = {}
            if fv.field == "definition" and fv.value is not None:
                patch["description"] = fv.value
            if fv.field == "tags" and fv.value is not None:
                patch["tags"] = list(fv.value) if isinstance(fv.value, list) else [fv.value]
            if patch:
                self.store.patch_definitions(
                    table, column_patches={column: patch}, decided_by=who,
                )
        elif fv.field == "classification":
            self.store.patch_column_privacy(
                table, column, classification=str(fv.value or ""), decided_by=who,
            )
        elif fv.field in ("entity_type", "logical_type", "masking", "quality_rules"):
            active = self.store.get_active(table)
            prop = {}
            for name, p in _iter_props(active or {}):
                if name == column:
                    prop = p
                    break
            fp = fingerprint_kwargs_from_prop(column, prop)
            existing = self.store.field_decisions.get_column(table, column).get(fv.field) or {}
            self.store.field_decisions.set(table, FieldDecision(
                column=column,
                field=fv.field,
                value=fv.value,
                decided_by=who,
                rationale_code=fv.rationale_code,
                chosen_source=fv.chosen_source,
                chosen_run_id=fv.chosen_run_id,
                decision_version=int(existing.get("decision_version") or 0) + 1,
                **fp,
            ))
        if fv.decision == "accept":
            self.review.approve_column(table, column, reviewer=who, note=fv.rationale_text)
        elif fv.decision == "edit":
            # edit_column already checkpointed as edited when pii/definition routed
            pass

    def _route_table_write(self, table: str, item: str, fv: FieldVerdict, who: str) -> None:
        if item in ("name", "description", "owner"):
            patch: dict[str, Any] = {}
            if item == "description" and fv.value is not None:
                patch["description"] = fv.value
            if item == "name" and fv.value is not None:
                patch["businessName"] = fv.value
            if patch:
                self.store.patch_definitions(table, table_patch=patch, decided_by=who)
        elif item.startswith("quality:"):
            rid = item.split(":", 1)[1]
            if fv.decision == "reject":
                from redibis.store.quality_decisions import QualityDecision
                self.store.quality_decisions.set(table, QualityDecision(
                    rule_id=rid, status="suppressed", decided_by=who,
                ))
                self.store._reapply_overlays(table, workflow="quality-decision", run_id="steward")

    def _append_human_generation(self, table: str, column: str, fv: FieldVerdict, who: str) -> None:
        active = self.store.get_active(table) or {}
        prop = {}
        for name, p in _iter_props(active):
            if name == column:
                prop = p
                break
        from redibis.review.fingerprint import fingerprint_from_contract_prop
        fp = fingerprint_from_contract_prop(column, prop)
        gen = Generation(
            field=fv.field,
            source="human",
            value=fv.value,
            confidence=1.0,
            run_id=fv.chosen_run_id or "steward",
            ts=fv.at or _utc_now_iso(),
            detail={
                "rationale_code": fv.rationale_code,
                "by": who,
                "decision": fv.decision,
            },
            fingerprint_key=fp.fingerprint_key,
        )
        self.ledger.append(table, column, [gen])
        fv.evidence_refs = list(fv.evidence_refs) + [gen.id]


def _coerce_pii(value: Any) -> bool:
    if isinstance(value, dict):
        if "is_pii" in value:
            return bool(value["is_pii"])
        if "detected" in value:
            return bool(value["detected"])
        if value.get("status") == "pii":
            return True
        if value.get("status") == "not_pii":
            return False
    return bool(value)


def _definition(prop: dict) -> str:
    business = prop.get("business")
    if isinstance(business, dict) and business.get("definition"):
        return str(business["definition"])
    for key in ("description", "businessName"):
        if prop.get(key):
            return str(prop[key])
    return ""


def _glossary_candidates(prop: dict, column: str) -> list[dict]:
    out = []
    for g in prop.get("authoritativeDefinitions") or prop.get("glossary") or []:
        if isinstance(g, dict):
            out.append({"term": g.get("term") or g.get("name") or "", "uri": g.get("uri") or ""})
        elif isinstance(g, str):
            out.append({"term": g, "uri": ""})
    if not out and column:
        out.append({"term": column.replace("_", " "), "uri": ""})
    return out


def _blocker_message(blockers: dict) -> str:
    parts = []
    if blockers.get("needs_review"):
        parts.append(f"{len(blockers['needs_review'])} needs review")
    if blockers.get("rejected"):
        parts.append(f"{len(blockers['rejected'])} rejected")
    if blockers.get("pending"):
        parts.append(f"{len(blockers['pending'])} pending")
    if blockers.get("table"):
        parts.append(f"{len(blockers['table'])} table items")
    return " · ".join(parts) or "blocked"
