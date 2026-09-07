"""Lineage graph + ODCS relationship helpers for Contract Synthesis.

Dataset lineage (reads/transforms) is kept distinct from ODCS foreign-key
``relationships[]`` and from profiling correlations / agent-run audit lineage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from redibis.synthesis.evidence import EvidenceBundle, EvidenceKind


@dataclass
class LineageNode:
    id: str
    kind: str  # table | column
    label: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LineageEdge:
    id: str
    source: str
    target: str
    kind: str  # reads_from | transforms_to | joins_on | foreign_key | lookup
    confidence: float = 1.0
    evidence_ids: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LineageGraph:
    nodes: list[LineageNode] = field(default_factory=list)
    edges: list[LineageEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    def to_react_flow(self) -> dict[str, Any]:
        """Shape compatible with the agents React Flow viewer."""
        nodes = []
        for i, n in enumerate(self.nodes):
            nodes.append({
                "id": n.id,
                "data": {"label": n.label, "kind": n.kind, **n.meta},
                "position": {"x": (i % 6) * 220, "y": (i // 6) * 120},
                "type": "default",
            })
        edges = []
        for e in self.edges:
            edges.append({
                "id": e.id,
                "source": e.source,
                "target": e.target,
                "label": e.kind,
                "data": {
                    "kind": e.kind,
                    "confidence": e.confidence,
                    "evidence_ids": e.evidence_ids,
                    **e.meta,
                },
            })
        return {"nodes": nodes, "edges": edges}


def build_lineage_graph(
    bundle: EvidenceBundle,
    *,
    target_table: str = "",
) -> LineageGraph:
    graph = LineageGraph()
    node_ids: set[str] = set()

    def add_table(name: str) -> str:
        nid = f"table:{name}"
        if nid not in node_ids:
            node_ids.add(nid)
            graph.nodes.append(LineageNode(id=nid, kind="table", label=name))
        return nid

    def add_column(table: str, column: str) -> str:
        nid = f"column:{table}.{column}" if table else f"column:{column}"
        if nid not in node_ids:
            node_ids.add(nid)
            graph.nodes.append(LineageNode(
                id=nid,
                kind="column",
                label=f"{table}.{column}" if table else column,
                meta={"table": table, "column": column},
            ))
        return nid

    target = target_table or ""
    if target:
        add_table(target)

    for rec in bundle.by_kind(EvidenceKind.TABLE_READ):
        table = str((rec.payload or {}).get("table") or "")
        if not table:
            continue
        src = add_table(table)
        if target:
            tgt = add_table(target)
            graph.edges.append(LineageEdge(
                id=f"edge:reads:{table}->{target}",
                source=src,
                target=tgt,
                kind="reads_from",
                confidence=rec.confidence,
                evidence_ids=[rec.id],
            ))

    for rec in bundle.by_kind(EvidenceKind.TABLE_WRITE):
        table = str((rec.payload or {}).get("table") or "")
        if table:
            add_table(table)
            if not target:
                target = table

    for rec in bundle.by_kind(EvidenceKind.COLUMN_TRANSFORM):
        col = str((rec.payload or {}).get("target_column") or "")
        if not col:
            continue
        tgt_col = add_column(target, col)
        sources = list((rec.payload or {}).get("transformSourceObjects") or [])
        src_cols = list((rec.payload or {}).get("source_columns") or [])
        if sources:
            for src_table in sources:
                src_n = add_table(str(src_table))
                graph.edges.append(LineageEdge(
                    id=f"edge:xf:{src_table}->{target}.{col}:{rec.id}",
                    source=src_n,
                    target=tgt_col,
                    kind="transforms_to",
                    confidence=rec.confidence,
                    evidence_ids=[rec.id],
                    meta={"transformLogic": (rec.payload or {}).get("transformLogic", "")[:300]},
                ))
        for sc in src_cols:
            # Ambiguous table — attach to first source or bare column node.
            src_table = sources[0] if sources else ""
            src_c = add_column(str(src_table), str(sc))
            graph.edges.append(LineageEdge(
                id=f"edge:col:{src_table}.{sc}->{target}.{col}:{rec.id}",
                source=src_c,
                target=tgt_col,
                kind="transforms_to",
                confidence=rec.confidence,
                evidence_ids=[rec.id],
            ))

    for rec in bundle.by_kind(EvidenceKind.JOIN):
        graph.edges.append(LineageEdge(
            id=f"edge:join:{rec.id}",
            source=add_table(target or "join"),
            target=add_table(target or "join"),
            kind="joins_on",
            confidence=rec.confidence,
            evidence_ids=[rec.id],
            meta={"sql": (rec.payload or {}).get("sql", "")[:200]},
        ))

    for rec in bundle.by_kind(EvidenceKind.FOREIGN_KEY):
        frm = str((rec.payload or {}).get("from") or "")
        to = str((rec.payload or {}).get("to") or "")
        if not to:
            continue
        # from/to as schema.property
        def split_ref(ref: str) -> tuple[str, str]:
            if "." in ref:
                a, b = ref.rsplit(".", 1)
                return a, b
            return target, ref

        ft, fc = split_ref(frm) if frm else (target, "")
        tt, tc = split_ref(to)
        src = add_column(ft, fc) if fc else add_table(ft or target)
        dst = add_column(tt, tc) if tc else add_table(tt)
        graph.edges.append(LineageEdge(
            id=f"edge:fk:{rec.id}",
            source=src,
            target=dst,
            kind="foreign_key",
            confidence=rec.confidence,
            evidence_ids=[rec.id],
        ))

    for rec in bundle.by_kind(EvidenceKind.LOOKUP):
        table = str((rec.payload or {}).get("table") or (rec.payload or {}).get("object") or "")
        if table and target:
            graph.edges.append(LineageEdge(
                id=f"edge:lookup:{table}->{target}:{rec.id}",
                source=add_table(table),
                target=add_table(target),
                kind="lookup",
                confidence=rec.confidence,
                evidence_ids=[rec.id],
            ))

    return graph


def apply_transform_fields(
    contract: dict[str, Any],
    bundle: EvidenceBundle,
) -> dict[str, Any]:
    """Write ``transformSourceObjects`` / ``transformLogic`` onto matching properties."""
    by_col: dict[str, list] = {}
    for rec in bundle.by_kind(EvidenceKind.COLUMN_TRANSFORM):
        col = (rec.payload or {}).get("target_column")
        if col:
            by_col.setdefault(str(col), []).append(rec)
    # Also requirements field derivations.
    for rec in bundle.by_kind(EvidenceKind.SCHEMA_FIELD):
        col = (rec.payload or {}).get("name")
        if not col:
            continue
        deriv = (rec.payload or {}).get("derivation") or ""
        sources_raw = (rec.payload or {}).get("sources") or ""
        sources = [s.strip() for s in str(sources_raw).replace("+", ",").split(",") if s.strip()]
        # Normalize SRC tokens out — keep object-like tokens.
        sources = [s for s in sources if "." in s or s.startswith("raw.") or s.startswith("ref.")]
        if deriv or sources:
            by_col.setdefault(str(col), []).append(rec)

    for schema_obj in contract.get("schema") or []:
        for prop in schema_obj.get("properties") or []:
            name = prop.get("name")
            if not name or name not in by_col:
                continue
            sources: list[str] = list(prop.get("transformSourceObjects") or [])
            logics: list[str] = []
            descs: list[str] = []
            for rec in by_col[str(name)]:
                payload = rec.payload or {}
                for s in payload.get("transformSourceObjects") or []:
                    if s and s not in sources:
                        sources.append(str(s))
                # requirements "sources" already normalized above into transformSourceObjects via list
                for s in payload.get("sources") and [] or []:
                    pass
                if payload.get("sources"):
                    for part in str(payload["sources"]).replace("+", ",").split(","):
                        part = part.strip().strip("`")
                        if "." in part and part not in sources:
                            sources.append(part)
                logic = payload.get("transformLogic") or payload.get("derivation") or ""
                if logic:
                    logics.append(str(logic))
                if payload.get("derivation"):
                    descs.append(str(payload["derivation"]))
            if sources:
                prop["transformSourceObjects"] = sources
            if logics:
                prop["transformLogic"] = logics[0][:2000]
            if descs and not prop.get("transformDescription"):
                prop["transformDescription"] = descs[0][:1000]
    return contract


def apply_foreign_key_relationships(
    contract: dict[str, Any],
    bundle: EvidenceBundle,
) -> dict[str, Any]:
    """Attach native ODCS v3.1 ``relationships[]`` for FOREIGN_KEY evidence only."""
    fks = bundle.by_kind(EvidenceKind.FOREIGN_KEY)
    if not fks:
        return contract
    for schema_obj in contract.get("schema") or []:
        rels = list(schema_obj.get("relationships") or [])
        for rec in fks:
            payload = rec.payload or {}
            entry: dict[str, Any] = {
                "type": payload.get("type") or "foreignKey",
                "to": payload.get("to"),
            }
            if payload.get("from"):
                entry["from"] = payload["from"]
            if payload.get("customProperties"):
                entry["customProperties"] = payload["customProperties"]
            if entry.get("to") and entry not in rels:
                rels.append(entry)
        schema_obj["relationships"] = rels
    return contract
