"""Spark pipeline analyzer — Python AST (parse-only) + conservative Scala patterns.

Never imports or executes uploaded code.
"""

from __future__ import annotations

import ast
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


def _const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _SparkPythonVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.records: list[EvidenceRecord] = []

    def _span(self, node: ast.AST) -> EvidenceSpan:
        return EvidenceSpan(
            path=self.path,
            start_line=getattr(node, "lineno", 0) or 0,
            end_line=getattr(node, "end_lineno", None) or getattr(node, "lineno", 0) or 0,
            extractor="spark_python",
        )

    def visit_Call(self, node: ast.Call) -> Any:
        func = node.func
        name = ""
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id

        # spark.table("x") / spark.read.table("x")
        if name in ("table",) and node.args:
            table = _const(node.args[0])
            if table:
                self.records.append(EvidenceRecord(
                    id=f"spark:read:{self.path}:{table}:{getattr(node, 'lineno', 0)}",
                    kind=EvidenceKind.TABLE_READ,
                    summary=f"spark.table({table})",
                    confidence=0.9,
                    payload={"table": table},
                    spans=[self._span(node)],
                    source="spark",
                ))

        # .write.saveAsTable / insertInto / parquet / etc.
        if name in ("saveAsTable", "insertInto"):
            table = _const(node.args[0]) if node.args else None
            if table:
                self.records.append(EvidenceRecord(
                    id=f"spark:write:{self.path}:{table}:{getattr(node, 'lineno', 0)}",
                    kind=EvidenceKind.TABLE_WRITE,
                    summary=f"write {name}({table})",
                    confidence=0.9,
                    payload={"table": table, "method": name},
                    spans=[self._span(node)],
                    source="spark",
                ))

        # spark.sql("...") — extract SQL text for secondary parse
        if name == "sql" and node.args:
            sql_text = _const(node.args[0])
            if sql_text:
                self.records.append(EvidenceRecord(
                    id=f"spark:sql:{self.path}:{getattr(node, 'lineno', 0)}",
                    kind=EvidenceKind.COLUMN_TRANSFORM,
                    summary="spark.sql(...) literal",
                    confidence=0.75,
                    payload={"sql": sql_text[:4000]},
                    spans=[self._span(node)],
                    source="spark",
                ))

        # withColumn("name", expr)
        if name == "withColumn" and len(node.args) >= 1:
            col = _const(node.args[0])
            expr = None
            if len(node.args) >= 2:
                try:
                    expr = ast.unparse(node.args[1])
                except Exception:
                    expr = None
            if col:
                self.records.append(EvidenceRecord(
                    id=f"spark:col:{self.path}:{col}:{getattr(node, 'lineno', 0)}",
                    kind=EvidenceKind.COLUMN_TRANSFORM,
                    summary=f"withColumn({col})",
                    confidence=0.85,
                    payload={
                        "target_column": col,
                        "transformLogic": (expr or "")[:1000],
                    },
                    spans=[self._span(node)],
                    source="spark",
                ))

        # select / selectExpr aliases are hard; capture selectExpr string args
        if name == "selectExpr":
            for arg in node.args:
                s = _const(arg)
                if not s:
                    continue
                # "expr AS alias"
                m = re.search(r"(?i)\bas\s+([A-Za-z_][\w]*)\s*$", s.strip())
                alias = m.group(1) if m else s
                self.records.append(EvidenceRecord(
                    id=f"spark:selectExpr:{self.path}:{alias}:{getattr(node, 'lineno', 0)}",
                    kind=EvidenceKind.COLUMN_TRANSFORM,
                    summary=f"selectExpr {s[:80]}",
                    confidence=0.7,
                    payload={"target_column": alias, "transformLogic": s[:1000]},
                    spans=[self._span(node)],
                    source="spark",
                ))

        # join
        if name == "join":
            self.records.append(EvidenceRecord(
                id=f"spark:join:{self.path}:{getattr(node, 'lineno', 0)}",
                kind=EvidenceKind.JOIN,
                summary="DataFrame.join(...)",
                confidence=0.7,
                payload={},
                spans=[self._span(node)],
                source="spark",
            ))

        self.generic_visit(node)


_SCALA_TABLE = re.compile(
    r"""(?x)
    (?:spark\.table\(\s*"([^"]+)"\s*\)
    |spark\.table\(\s*'([^']+)'\s*\)
    |\.saveAsTable\(\s*"([^"]+)"\s*\)
    |\.insertInto\(\s*"([^"]+)"\s*\)
    |CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+([`\w\.]+)
    )
    """,
    re.I,
)
_SCALA_WITH_COLUMN = re.compile(
    r"""\.withColumn\(\s*"([^"]+)"\s*,""",
)


class SparkAnalyzer:
    name = "spark"

    def supports(self, file: IngestedFile) -> bool:
        return file.kind == "spark" or file.relpath.lower().endswith((".py", ".scala"))

    def analyze(self, file: IngestedFile, bundle: EvidenceBundle) -> None:
        text = file.text or ""
        if file.relpath.lower().endswith(".py"):
            self._analyze_python(file, text, bundle)
            # Also parse any spark.sql literals through sql analyzer helpers.
            self._follow_sql_literals(file, bundle)
        else:
            self._analyze_scala(file, text, bundle)

    def _analyze_python(self, file: IngestedFile, text: str, bundle: EvidenceBundle) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            bundle.unresolved.append(EvidenceRecord(
                id=f"spark:parse-error:{file.relpath}",
                kind=EvidenceKind.UNRESOLVED,
                summary=f"Python parse failed: {exc}",
                confidence=0.0,
                spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                source="spark",
            ))
            # Fallback regex for table names.
            self._analyze_scala(file, text, bundle)
            return
        visitor = _SparkPythonVisitor(file.relpath)
        visitor.visit(tree)
        for rec in visitor.records:
            bundle.add(rec)

    def _follow_sql_literals(self, file: IngestedFile, bundle: EvidenceBundle) -> None:
        from redibis.synthesis.analyzers.sql import SqlAnalyzer

        sql_a = SqlAnalyzer()
        for rec in list(bundle.records):
            if rec.source != "spark":
                continue
            sql = (rec.payload or {}).get("sql")
            if not sql:
                continue
            fake = IngestedFile(
                relpath=f"{file.relpath}#sql",
                kind="sql",
                sha256="",
                size=len(sql),
                text=sql,
            )
            sql_a.analyze(fake, bundle)

    def _analyze_scala(self, file: IngestedFile, text: str, bundle: EvidenceBundle) -> None:
        for m in _SCALA_TABLE.finditer(text):
            table = next((g for g in m.groups() if g), None)
            if not table:
                continue
            table = table.strip('`"')
            kind = EvidenceKind.TABLE_WRITE if "saveAsTable" in m.group(0) or "insertInto" in m.group(0) or "CREATE" in m.group(0).upper() else EvidenceKind.TABLE_READ
            line = text.count("\n", 0, m.start()) + 1
            bundle.add(EvidenceRecord(
                id=f"spark:scala:{kind.value}:{file.relpath}:{table}:{line}",
                kind=kind,
                summary=f"{kind.value} {table}",
                confidence=0.7,
                payload={"table": table},
                spans=[EvidenceSpan(
                    path=file.relpath, start_line=line, end_line=line, extractor=self.name,
                )],
                source="spark",
            ))
        for m in _SCALA_WITH_COLUMN.finditer(text):
            col = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            bundle.add(EvidenceRecord(
                id=f"spark:scala:col:{file.relpath}:{col}:{line}",
                kind=EvidenceKind.COLUMN_TRANSFORM,
                summary=f"withColumn({col})",
                confidence=0.65,
                payload={"target_column": col},
                spans=[EvidenceSpan(
                    path=file.relpath, start_line=line, end_line=line, extractor=self.name,
                )],
                source="spark",
            ))


register_analyzer(SparkAnalyzer())
