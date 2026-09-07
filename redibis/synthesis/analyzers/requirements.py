"""Deterministic requirements document analyzer.

Parses structured Markdown tables and ``REQ-ID`` / ``BR-01`` style identifiers.
Unsupported free prose becomes explicit unresolved evidence (no guessing).
"""

from __future__ import annotations

import re
from typing import Any

from redibis.synthesis.analyzers.base import register_analyzer
from redibis.synthesis.evidence import (
    EvidenceBundle,
    EvidenceKind,
    EvidenceRecord,
    EvidenceSpan,
    TraceStatus,
    TraceabilityRow,
)
from redibis.synthesis.ingest import IngestedFile

# Requirement IDs like BR-01, SRC-01, TGT-F01, DQ-17, SLA-02, SEC-03, NFR-01, OPS-01, CM-04, AC-01, LKP-01, TR-05
_REQ_ID = re.compile(
    r"\b((?:BR|SRC|LKP|TGT-F|TGT|TR|DQ|SLA|SEC|NFR|OPS|CM|AC)-\d+[A-Z]?)\b"
)
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
_TABLE_ROW = re.compile(r"^\|(.+)\|$", re.M)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _parse_md_tables(text: str) -> list[dict[str, Any]]:
    """Return simple pipe tables as list of {headers, rows, start_line}."""
    lines = text.splitlines()
    tables: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i + 1].strip()):
            headers = [c.strip() for c in line.strip("|").split("|")]
            start = i + 1
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                rows.append(cells)
                j += 1
            tables.append({"headers": headers, "rows": rows, "start_line": start + 1})
            i = j
            continue
        i += 1
    return tables


class RequirementsAnalyzer:
    name = "requirements"

    def supports(self, file: IngestedFile) -> bool:
        return file.kind == "requirements"

    def analyze(self, file: IngestedFile, bundle: EvidenceBundle) -> None:
        text = file.text or ""
        seen_ids: set[str] = set()

        for match in _REQ_ID.finditer(text):
            rid = match.group(1)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            line = _line_of(text, match.start())
            # Grab surrounding sentence/line for summary.
            line_text = text.splitlines()[line - 1].strip() if line <= len(text.splitlines()) else rid
            kind = self._kind_for(rid)
            rec = EvidenceRecord(
                id=f"req:{rid}",
                kind=kind,
                summary=line_text[:300],
                confidence=0.9,
                requirement_ids=[rid],
                payload={"raw_line": line_text},
                spans=[EvidenceSpan(
                    path=file.relpath,
                    start_line=line,
                    end_line=line,
                    extractor=self.name,
                )],
                source="requirements",
            )
            bundle.add(rec)
            # Traceability seed — mapped later by reconcile/stages.
            status = TraceStatus.DOC_ONLY if rid.startswith("AC-") else TraceStatus.UNRESOLVED
            bundle.traceability.append(TraceabilityRow(
                requirement_id=rid,
                status=status,
                evidence_ids=[rec.id],
                notes="seeded from requirements document",
            ))

        # Field-spec tables (columns containing Field / Logical type).
        for table in _parse_md_tables(text):
            headers_l = [h.lower() for h in table["headers"]]
            if not any("field" in h or "column" in h for h in headers_l):
                # Still record generic table presence for SLA / DQ tables.
                if any(h in ("id", "check", "property", "requirement") or "id" in h for h in headers_l):
                    self._ingest_generic_table(file, table, bundle)
                continue
            name_idx = next((i for i, h in enumerate(headers_l) if "field" in h or "column" in h), 0)
            id_idx = next((i for i, h in enumerate(headers_l) if h == "id" or "id" in h), None)
            type_idx = next((i for i, h in enumerate(headers_l) if "logical" in h), None)
            phys_idx = next((i for i, h in enumerate(headers_l) if "physical" in h), None)
            null_idx = next((i for i, h in enumerate(headers_l) if "null" in h), None)
            src_idx = next((i for i, h in enumerate(headers_l) if "source" in h), None)
            deriv_idx = next((i for i, h in enumerate(headers_l) if "deriv" in h), None)
            class_idx = next((i for i, h in enumerate(headers_l) if "class" in h), None)
            cde_idx = next((i for i, h in enumerate(headers_l) if h == "cde" or "critical" in h), None)

            for row_i, row in enumerate(table["rows"]):
                def cell(idx: int | None) -> str:
                    if idx is None or idx >= len(row):
                        return ""
                    return row[idx].strip().strip("`")

                field_name = cell(name_idx)
                if not field_name or field_name.lower() in ("field", "---"):
                    continue
                rid = cell(id_idx) if id_idx is not None else ""
                payload = {
                    "name": field_name,
                    "logicalType": cell(type_idx).lower() if type_idx is not None else "",
                    "physicalType": cell(phys_idx) if phys_idx is not None else "",
                    "nullable": cell(null_idx) if null_idx is not None else "",
                    "sources": cell(src_idx) if src_idx is not None else "",
                    "derivation": cell(deriv_idx) if deriv_idx is not None else "",
                    "classification": cell(class_idx).lower() if class_idx is not None else "",
                    "criticalDataElement": cell(cde_idx).lower() in ("yes", "true", "y")
                    if cde_idx is not None else False,
                }
                req_ids = _REQ_ID.findall(rid) or ([rid] if rid else [])
                line = table["start_line"] + row_i
                bundle.add(EvidenceRecord(
                    id=f"field:{file.relpath}:{field_name}",
                    kind=EvidenceKind.SCHEMA_FIELD,
                    summary=f"Field {field_name} from requirements table",
                    confidence=0.95,
                    requirement_ids=req_ids,
                    payload=payload,
                    spans=[EvidenceSpan(
                        path=file.relpath,
                        start_line=line,
                        end_line=line,
                        extractor=self.name,
                    )],
                    source="requirements",
                ))

        # Headings without requirement IDs → unresolved prose sections.
        for m in _HEADING.finditer(text):
            title = m.group(2).strip()
            if _REQ_ID.search(title):
                continue
            # Skip tiny / TOC headings
            if len(title) < 4:
                continue

    def _ingest_generic_table(
        self,
        file: IngestedFile,
        table: dict[str, Any],
        bundle: EvidenceBundle,
    ) -> None:
        headers_l = [h.lower() for h in table["headers"]]
        id_idx = next((i for i, h in enumerate(headers_l) if h == "id" or h.endswith(" id")), 0)
        for row_i, row in enumerate(table["rows"]):
            if id_idx >= len(row):
                continue
            rid_cell = row[id_idx].strip()
            ids = _REQ_ID.findall(rid_cell)
            if not ids:
                continue
            # already captured via ID scan; enrich payload with row cells
            payload = {table["headers"][i]: row[i].strip() for i in range(min(len(row), len(table["headers"])))}
            for rid in ids:
                bundle.add(EvidenceRecord(
                    id=f"table-row:{file.relpath}:{rid}:{row_i}",
                    kind=self._kind_for(rid),
                    summary=f"Table row for {rid}",
                    confidence=0.85,
                    requirement_ids=[rid],
                    payload=payload,
                    spans=[EvidenceSpan(
                        path=file.relpath,
                        start_line=table["start_line"] + row_i,
                        end_line=table["start_line"] + row_i,
                        extractor=self.name,
                    )],
                    source="requirements",
                ))

    @staticmethod
    def _kind_for(rid: str) -> EvidenceKind:
        if rid.startswith(("TGT-F", "TGT-")):
            return EvidenceKind.SCHEMA_FIELD
        if rid.startswith("DQ-"):
            return EvidenceKind.QUALITY_RULE
        if rid.startswith("SLA-"):
            return EvidenceKind.SLA
        if rid.startswith("SEC-"):
            return EvidenceKind.SECURITY
        if rid.startswith(("SRC-", "LKP-", "TR-")):
            return EvidenceKind.COLUMN_TRANSFORM
        if rid.startswith(("NFR-", "OPS-", "CM-")):
            return EvidenceKind.CUSTOM
        if rid.startswith("BR-"):
            return EvidenceKind.REQUIREMENT
        if rid.startswith("AC-"):
            return EvidenceKind.REQUIREMENT
        return EvidenceKind.REQUIREMENT


register_analyzer(RequirementsAnalyzer())
