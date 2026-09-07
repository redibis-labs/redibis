"""SQL source analyzer using sqlglot (optional dependency)."""

from __future__ import annotations

import re
from typing import Any, Optional

from redibis.synthesis.analyzers.base import register_analyzer
from redibis.synthesis.evidence import (
    EvidenceBundle,
    EvidenceKind,
    EvidenceRecord,
    EvidenceSpan,
)
from redibis.synthesis.ingest import IngestedFile


def _sqlglot_available() -> bool:
    try:
        import sqlglot  # noqa: F401
        return True
    except ImportError:
        return False


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, max(pos, 0)) + 1


class SqlAnalyzer:
    name = "sql"

    def supports(self, file: IngestedFile) -> bool:
        return file.kind == "sql" or file.relpath.lower().endswith(".sql")

    def analyze(self, file: IngestedFile, bundle: EvidenceBundle) -> None:
        text = file.text or ""
        if not text.strip():
            return
        if _sqlglot_available():
            self._analyze_sqlglot(file, text, bundle)
        else:
            self._analyze_regex(file, text, bundle)

    def _analyze_sqlglot(self, file: IngestedFile, text: str, bundle: EvidenceBundle) -> None:
        import sqlglot
        from sqlglot import exp

        try:
            statements = sqlglot.parse(text, read=None)
        except Exception:
            # Fall back dialect by dialect.
            statements = []
            for dialect in ("spark", "hive", "databricks", "ansi"):
                try:
                    statements = sqlglot.parse(text, read=dialect)
                    break
                except Exception:
                    continue
            if not statements:
                self._analyze_regex(file, text, bundle)
                return

        for idx, tree in enumerate(statements):
            if tree is None:
                continue
            # Write targets
            for node in tree.find_all(exp.Table):
                # Heuristic: INSERT/CREATE targets vs FROM sources via parent.
                table_name = self._table_name(node)
                if not table_name:
                    continue
                parent = node.parent
                is_write = isinstance(
                    parent,
                    (exp.Insert, exp.Create, exp.Delete, exp.Update, exp.Merge),
                ) or (
                    parent is not None
                    and isinstance(getattr(parent, "this", None), exp.Table)
                    and parent.this is node
                    and isinstance(parent, (exp.Insert, exp.Create))
                )
                # Better write detection:
                kind = EvidenceKind.TABLE_READ
                if isinstance(tree, (exp.Insert, exp.Create)):
                    # First table under Insert/Create is often the target.
                    target = tree.this
                    if isinstance(target, exp.Schema):
                        target = target.this
                    if target is node or (
                        isinstance(target, exp.Table) and self._table_name(target) == table_name
                    ):
                        kind = EvidenceKind.TABLE_WRITE
                elif isinstance(tree, exp.Create):
                    kind = EvidenceKind.TABLE_WRITE

                # Simpler: check SQL keywords near table for INSERT INTO / CREATE TABLE
                # Prefer structural:
                if isinstance(tree, exp.Insert):
                    tgt = tree.this
                    if isinstance(tgt, exp.Schema):
                        tgt = tgt.this
                    if isinstance(tgt, exp.Table) and self._table_name(tgt) == table_name:
                        kind = EvidenceKind.TABLE_WRITE
                if isinstance(tree, exp.Create):
                    tgt = tree.this
                    if isinstance(tgt, exp.Schema):
                        tgt = tgt.this
                    if isinstance(tgt, exp.Table) and self._table_name(tgt) == table_name:
                        kind = EvidenceKind.TABLE_WRITE

                bundle.add(EvidenceRecord(
                    id=f"sql:{file.relpath}:{kind.value}:{table_name}:{idx}",
                    kind=kind,
                    summary=f"{kind.value} {table_name}",
                    confidence=0.9,
                    payload={"table": table_name, "dialect": "sqlglot"},
                    spans=[EvidenceSpan(
                        path=file.relpath,
                        start_line=getattr(node, "meta", {}) and 0 or 0,
                        extractor=self.name,
                    )],
                    source="sql",
                ))

            for join in tree.find_all(exp.Join):
                on = join.args.get("on")
                bundle.add(EvidenceRecord(
                    id=f"sql:join:{file.relpath}:{idx}:{id(join)}",
                    kind=EvidenceKind.JOIN,
                    summary=f"JOIN {join.sql()[:120]}",
                    confidence=0.85,
                    payload={
                        "sql": join.sql()[:500],
                        "on": on.sql() if on is not None else "",
                    },
                    spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                    source="sql",
                ))

            # SELECT projections as column transforms when alias present.
            for sel in tree.find_all(exp.Select):
                for proj in sel.expressions:
                    alias = proj.alias_or_name
                    expr_sql = proj.sql()
                    if not alias:
                        continue
                    # Collect source columns referenced.
                    cols = [c.name for c in proj.find_all(exp.Column) if c.name]
                    tables = sorted({
                        self._table_name(t)
                        for t in proj.find_all(exp.Table)
                        if self._table_name(t)
                    })
                    # Also tables from FROM clause of select
                    for t in sel.find_all(exp.Table):
                        tn = self._table_name(t)
                        if tn:
                            tables.append(tn)
                    tables = sorted(set(tables))
                    bundle.add(EvidenceRecord(
                        id=f"sql:col:{file.relpath}:{alias}:{idx}",
                        kind=EvidenceKind.COLUMN_TRANSFORM,
                        summary=f"column {alias} <- {expr_sql[:100]}",
                        confidence=0.8,
                        payload={
                            "target_column": alias,
                            "transformLogic": expr_sql[:1000],
                            "source_columns": cols,
                            "transformSourceObjects": tables,
                        },
                        spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                        source="sql",
                    ))

    def _analyze_regex(self, file: IngestedFile, text: str, bundle: EvidenceBundle) -> None:
        for m in re.finditer(
            r"(?is)\b(insert\s+into|create\s+(?:or\s+replace\s+)?table|merge\s+into)\s+([`\"\w\.]+)",
            text,
        ):
            table = m.group(2).strip('`"')
            line = _line_of(text, m.start())
            bundle.add(EvidenceRecord(
                id=f"sql:write:{file.relpath}:{table}:{line}",
                kind=EvidenceKind.TABLE_WRITE,
                summary=f"table_write {table}",
                confidence=0.7,
                payload={"table": table},
                spans=[EvidenceSpan(
                    path=file.relpath, start_line=line, end_line=line, extractor=self.name,
                )],
                source="sql",
            ))
        for m in re.finditer(r"(?is)\b(?:from|join)\s+([`\"\w\.]+)", text):
            table = m.group(1).strip('`"')
            line = _line_of(text, m.start())
            bundle.add(EvidenceRecord(
                id=f"sql:read:{file.relpath}:{table}:{line}",
                kind=EvidenceKind.TABLE_READ,
                summary=f"table_read {table}",
                confidence=0.65,
                payload={"table": table},
                spans=[EvidenceSpan(
                    path=file.relpath, start_line=line, end_line=line, extractor=self.name,
                )],
                source="sql",
            ))

    @staticmethod
    def _table_name(node: Any) -> str:
        try:
            catalog = node.catalog
            db = node.db
            name = node.name
            parts = [p for p in (catalog, db, name) if p]
            return ".".join(str(p) for p in parts)
        except Exception:
            try:
                return str(node)
            except Exception:
                return ""


register_analyzer(SqlAnalyzer())
