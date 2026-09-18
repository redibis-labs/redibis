"""A5 — knowledge graph of the reviewed contract (JSON-LD + nodes/edges). No sample values."""

from __future__ import annotations

from typing import Any

from redibis.store.review_store import ReviewState


def build_review_graph(svc, table: str, a1: dict, a2: dict, digest: str) -> dict:
    active = svc.store.get_active(table) or {}
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def node(nid: str, ntype: str, **props: Any) -> str:
        if nid not in seen:
            nodes.append({"id": nid, "type": ntype, **props})
            seen.add(nid)
        return nid

    table_id = f"table:{table}"
    node(table_id, "Table", name=table)

    schema0 = (active.get("schema") or [{}])[0] if active.get("schema") else {}
    for prop in schema0.get("properties") or []:
        if not isinstance(prop, dict) or not prop.get("name"):
            continue
        col = str(prop["name"])
        col_id = f"column:{table}.{col}"
        node(col_id, "Column", name=col, table=table)
        edges.append({"from": table_id, "to": col_id, "type": "HAS_COLUMN"})
        cls = str((prop.get("privacy") or {}).get("classification") or prop.get("classification") or "")
        if cls:
            cid = f"classification:{cls}"
            node(cid, "Classification", name=cls)
            edges.append({"from": col_id, "to": cid, "type": "CLASSIFIED_AS"})
        entity = str(prop.get("entity_type") or "")
        if entity:
            eid = f"entity:{entity}"
            node(eid, "EntityType", name=entity)
            edges.append({"from": col_id, "to": eid, "type": "IS_ENTITY"})
        definition = str(prop.get("description") or "")
        if definition:
            did = f"definition:{table}.{col}"
            node(did, "Definition", text=definition)
            edges.append({"from": col_id, "to": did, "type": "DEFINED_BY"})
        for q in prop.get("quality") or []:
            if not isinstance(q, dict):
                continue
            qid = f"quality:{q.get('type') or q.get('rule') or 'rule'}:{col}"
            node(qid, "QualityRule", column=col, rule=q.get("type") or q.get("rule"))
            edges.append({"from": col_id, "to": qid, "type": "GOVERNED_BY"})

    try:
        from redibis.profiling.relationships import RELATIONSHIP_KEYS  # noqa: F401
    except Exception:
        pass

    for entry in a1.get("entries") or []:
        col = entry.get("column")
        if not col:
            continue
        col_id = f"column:{table}.{col}"
        sid = f"decision:{table}.{col}.{entry.get('field')}"
        node(sid, "StewardDecision",
             field=entry.get("field"),
             decision=entry.get("decision"),
             rationale_code=entry.get("rationale_code"),
             chosen_source=entry.get("chosen_source"))
        edges.append({"from": sid, "to": col_id, "type": "DECIDES"})
        run_id = entry.get("chosen_run_id")
        if run_id:
            rid = f"run:{run_id}"
            node(rid, "Run", run_id=run_id)
            if entry.get("chosen_source") == "human":
                edges.append({"from": sid, "to": rid, "type": "OVERRODE"})

    jsonld = {
        "@context": {
            "name": "http://schema.org/name",
            "HAS_COLUMN": "https://redibis.local/rel/HAS_COLUMN",
            "CLASSIFIED_AS": "https://redibis.local/rel/CLASSIFIED_AS",
            "IS_ENTITY": "https://redibis.local/rel/IS_ENTITY",
            "DEFINED_BY": "https://redibis.local/rel/DEFINED_BY",
            "GOVERNED_BY": "https://redibis.local/rel/GOVERNED_BY",
            "DECIDES": "https://redibis.local/rel/DECIDES",
            "OVERRODE": "https://redibis.local/rel/OVERRODE",
            "REDUNDANT_WITH": "https://redibis.local/rel/REDUNDANT_WITH",
            "REFERENCES": "https://redibis.local/rel/REFERENCES",
            "DERIVED_FROM": "https://redibis.local/rel/DERIVED_FROM",
            "USES_TERM": "https://redibis.local/rel/USES_TERM",
        },
        "@graph": [
            {"@id": n["id"], "@type": n["type"], **{k: v for k, v in n.items() if k not in ("id", "type")}}
            for n in nodes
        ],
        "review_digest": digest,
        "residency": "portable",
    }
    return {
        "jsonld": jsonld,
        "plain": {"nodes": nodes, "edges": edges, "review_digest": digest, "residency": "portable"},
    }
