"""Steward review artifacts A0–A5 — derived, not authored.

Written under ``_meta/steward_artifacts/{table}/{review_digest}/``.
Re-exporting the same inputs yields the same digest identity; each artifact
carries ``review_digest``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

from redibis.store.contract_store import ContractStore
from redibis.store.review_store import REVIEWED_DECISIONS, ReviewState

ARTIFACT_NAMES = (
    "contract.reviewed.json",
    "steward_verdicts.json",
    "review_evidence.json",
    "llm_context",
    "finetune",
    "graph.jsonld",
    "graph.json",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def review_digest(contract_uuid: str, verdict_ids: list[str], ts: str) -> str:
    payload = json.dumps(
        {"uuid": contract_uuid, "verdicts": sorted(verdict_ids), "ts": ts},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prefix(table: str, digest: str) -> str:
    return f"_meta/steward_artifacts/{table}/{digest}"


def _verdict_ids(state: ReviewState) -> list[str]:
    ids: list[str] = []
    for col, cr in state.columns.items():
        for field, fv in (cr.verdicts or {}).items():
            ids.append(f"{col}:{field}:{fv.decision}:{fv.at}")
        if not cr.verdicts:
            ids.append(f"{col}:{cr.status}:{cr.reviewed_at}")
    for item, fv in state.table_review.items.items():
        ids.append(f"table:{item}:{fv.decision}:{fv.at}")
    return ids


def write_steward_artifacts(svc, table: str, *, actor: str, state: ReviewState) -> dict:
    """Write A0–A5 and return a listing with digest + paths."""
    from redibis.review.graph import build_review_graph
    from redibis.review.llm_context import write_llm_context
    from redibis.training.exporter import ExportOptions, TrainingDatasetExporter

    store: ContractStore = svc.store
    active = store.get_active(table)
    if active is None:
        raise ValueError(f"No active contract for {table!r}")
    ts = _utc_now_iso()
    digest = review_digest(str(active.get("contract_uuid") or state.contract_uuid or ""),
                           _verdict_ids(state), ts)
    prefix = _prefix(table, digest)
    backend, bucket = store.backend, store.bucket

    from redibis.contracts.privacy import strip_quality_from_pii_columns

    a0 = copy.deepcopy(active)
    strip_quality_from_pii_columns(a0)
    a0["x-redibis-review"] = {
        "status": "guaranteed" if state.guaranteed else "reviewed",
        "review_digest": digest,
        "finalized_by": actor,
        "finalized_at": ts,
        "reviewed_columns": state.reviewed_count,
        "table_reviewed": bool(state.table_review.items),
        "artifacts": {
            "verdict_memory": f"{prefix}/steward_verdicts.json",
            "evidence": f"{prefix}/review_evidence.json",
            "llm_context": f"{prefix}/llm_context/",
            "corpus": f"{prefix}/finetune/",
            "graph": f"{prefix}/graph.jsonld",
        },
    }
    backend.put_json(bucket, f"{prefix}/contract.reviewed.json", a0)

    a1 = _build_verdict_memory(svc, table, state, digest, ts, actor)
    backend.put_json(bucket, f"{prefix}/steward_verdicts.json", a1)

    a2 = _build_evidence(svc, table, state, digest, ts, actor)
    backend.put_json(bucket, f"{prefix}/review_evidence.json", a2)

    llm_files = write_llm_context(svc, table, state, digest, a1, a2)
    for name, body in llm_files.items():
        key = f"{prefix}/llm_context/{name}"
        if name.endswith(".json"):
            backend.put_json(bucket, key, body)
        else:
            backend.put_text(bucket, key, body if isinstance(body, str) else json.dumps(body))

    exporter = TrainingDatasetExporter(store, review_store=svc.reviews)
    corpus = exporter.export_steward_bundle(
        table,
        digest=digest,
        profiles=svc.profiles,
        consent=store.sampling_consent,
    )
    for name, payload in corpus.items():
        key = f"{prefix}/finetune/{name}"
        if name.endswith(".jsonl"):
            backend.put_text(bucket, key, payload if isinstance(payload, str) else payload)
        else:
            backend.put_json(bucket, key, payload)

    graph = build_review_graph(svc, table, a1, a2, digest)
    backend.put_json(bucket, f"{prefix}/graph.jsonld", graph["jsonld"])
    backend.put_json(bucket, f"{prefix}/graph.json", graph["plain"])

    listing = {
        "review_digest": digest,
        "table": table,
        "prefix": prefix,
        "finalized_at": ts,
        "finalized_by": actor,
        "artifacts": {
            "contract": f"{prefix}/contract.reviewed.json",
            "verdicts": f"{prefix}/steward_verdicts.json",
            "evidence": f"{prefix}/review_evidence.json",
            "llm_context": f"{prefix}/llm_context/",
            "corpus": f"{prefix}/finetune/",
            "graph": f"{prefix}/graph.jsonld",
        },
    }
    backend.put_json(bucket, f"{prefix}/manifest.json", listing)
    backend.put_json(bucket, f"_meta/steward_artifacts/{table}/latest.json", listing)
    return listing


def list_artifacts(store: ContractStore, table: str) -> dict:
    latest_key = f"_meta/steward_artifacts/{table}/latest.json"
    if not store.backend.exists(store.bucket, latest_key):
        return {"table": table, "artifacts": [], "review_digest": None}
    latest = store.backend.get_json(store.bucket, latest_key) or {}
    return latest


def get_artifact(store: ContractStore, table: str, name: str) -> tuple[bytes, str, str]:
    listing = list_artifacts(store, table)
    prefix = listing.get("prefix") or ""
    if not prefix:
        raise ValueError(f"No steward artifacts for {table!r}")
    mapping = {
        "contract": "contract.reviewed.json",
        "contract.reviewed.json": "contract.reviewed.json",
        "a0": "contract.reviewed.json",
        "verdicts": "steward_verdicts.json",
        "steward_verdicts.json": "steward_verdicts.json",
        "a1": "steward_verdicts.json",
        "evidence": "review_evidence.json",
        "review_evidence.json": "review_evidence.json",
        "a2": "review_evidence.json",
        "graph": "graph.jsonld",
        "graph.jsonld": "graph.jsonld",
        "a5": "graph.jsonld",
        "graph.json": "graph.json",
        "llm-context": "llm_context/context.json",
        "llm_context": "llm_context/context.json",
        "corpus": "finetune/manifest.json",
        "finetune": "finetune/manifest.json",
    }
    rel = mapping.get(name) or name
    key = f"{prefix}/{rel}"
    if not store.backend.exists(store.bucket, key):
        raise ValueError(f"artifact {name!r} not found")
    if rel.endswith(".jsonl") or rel.endswith(".md"):
        text = store.backend.get_text(store.bucket, key) or ""
        return text.encode("utf-8"), "text/plain", rel.split("/")[-1]
    data = store.backend.get_json(store.bucket, key)
    body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    return body, "application/json", rel.split("/")[-1]


def export_artifact_bundle(
    store: ContractStore,
    table: str,
    *,
    current_verdicts: Optional[dict] = None,
) -> bytes:
    """Zip current A1 plus every finalized A0–A5 object under the latest digest."""
    import yaml

    buf = BytesIO()
    safe = table.replace("/", "_")
    listing = list_artifacts(store, table)
    prefix = str(listing.get("prefix") or "")
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if current_verdicts:
            zf.writestr(
                f"{safe}/current/steward_verdicts.json",
                json.dumps(current_verdicts, indent=2, ensure_ascii=False, default=str),
            )
        active = store.get_active(table)
        if active:
            zf.writestr(
                f"{safe}/current/contract.yaml",
                yaml.safe_dump(active, sort_keys=False, allow_unicode=True),
            )
        if listing:
            zf.writestr(
                f"{safe}/latest.json",
                json.dumps(listing, indent=2, ensure_ascii=False, default=str),
            )
        if prefix:
            try:
                keys = store.backend.list_keys(store.bucket, prefix=prefix)
            except Exception:
                keys = []
            for key in keys:
                rel = str(key)[len(prefix):].lstrip("/")
                if not rel:
                    continue
                try:
                    body = store.backend.get_bytes(store.bucket, key)
                except Exception:
                    try:
                        text = store.backend.get_text(store.bucket, key) or ""
                        body = text.encode("utf-8")
                    except Exception:
                        continue
                zf.writestr(f"{safe}/{rel}", body)
    return buf.getvalue()


def _build_verdict_memory(svc, table: str, state: ReviewState, digest: str, ts: str, actor: str) -> dict:
    """A1 — value-free, fingerprint-gated, importable."""
    entries = []
    pii = svc.store.get_pii_decisions(table)
    field_decs = svc.store.field_decisions.get(table)
    for col, cr in state.columns.items():
        fp = ""
        pii_d = pii.get(col) or {}
        if pii_d.get("fingerprint_key"):
            fp = pii_d["fingerprint_key"]
        for field, fv in (cr.verdicts or {}).items():
            fd = (field_decs.get(col) or {}).get(field) or {}
            entries.append({
                "table": table,
                "column": col,
                "field": field,
                "decision": fv.decision,
                "chosen_source": fv.chosen_source,
                "chosen_run_id": fv.chosen_run_id,
                "value": _portable_value(fv.value),
                "rationale_code": fv.rationale_code,
                "fingerprint_key": fd.get("fingerprint_key") or fp,
                "lifecycle_state": fd.get("lifecycle_state") or pii_d.get("lifecycle_state") or "active",
                "decision_version": fd.get("decision_version") or pii_d.get("decision_version") or 1,
                "status": cr.status,
            })
        if not cr.verdicts:
            entries.append({
                "table": table,
                "column": col,
                "field": "column",
                "decision": cr.status,
                "chosen_source": "",
                "value": None,
                "rationale_code": "",
                "fingerprint_key": fp,
                "lifecycle_state": pii_d.get("lifecycle_state") or "active",
                "decision_version": pii_d.get("decision_version") or 1,
                "status": cr.status,
            })
    for item, fv in state.table_review.items.items():
        entries.append({
            "table": table,
            "column": None,
            "item": item,
            "field": item,
            "decision": fv.decision,
            "chosen_source": fv.chosen_source,
            "value": _portable_value(fv.value),
            "rationale_code": fv.rationale_code,
            "fingerprint_key": "",
            "lifecycle_state": "active",
            "decision_version": 1,
        })
    return {
        "kind": "redibis.steward_verdicts",
        "schema_version": "1.0",
        "review_digest": digest,
        "exported_at": ts,
        "exporter": actor,
        "residency": "portable",
        "table": table,
        "entries": entries,
    }


def _build_evidence(svc, table: str, state: ReviewState, digest: str, ts: str, actor: str) -> dict:
    from redibis.contracts.privacy import scrub_pii_text

    decisions = []
    for col, cr in state.columns.items():
        gens = [g.to_dict() for g in svc.ledger.get(table, col)]
        for g in gens:
            detail = dict(g.get("detail") or {})
            if detail.get("reasoning"):
                detail["reasoning"] = scrub_pii_text(str(detail["reasoning"]))
            g["detail"] = detail
            g["value"] = _portable_value(g.get("value"))
        for field, fv in (cr.verdicts or {}).items():
            decisions.append({
                "column": col,
                "field": field,
                "decision": fv.decision,
                "chosen_source": fv.chosen_source,
                "chosen_run_id": fv.chosen_run_id,
                "rationale_code": fv.rationale_code,
                "rationale_text": scrub_pii_text(fv.rationale_text or ""),
                "evidence_refs": list(fv.evidence_refs),
                "generations": [g for g in gens if g.get("field") == field],
                "reviewer": fv.by or cr.reviewed_by,
                "at": fv.at or cr.reviewed_at,
            })
    return {
        "kind": "redibis.review_evidence",
        "review_digest": digest,
        "exported_at": ts,
        "exporter": actor,
        "residency": "portable",
        "table": table,
        "contract_uuid": state.contract_uuid,
        "decisions": decisions,
    }


def _portable_value(value: Any) -> Any:
    """Drop sample-like payloads from A1/A2 and scrub remaining free text."""
    from redibis.contracts.privacy import scrub_pii_text

    if isinstance(value, dict):
        return {k: _portable_value(v) for k, v in value.items()
                if k not in ("samples", "sample", "values", "reasoning")}
    if isinstance(value, list) and value and isinstance(value[0], str):
        return None
    if isinstance(value, str):
        return scrub_pii_text(value)
    return value
