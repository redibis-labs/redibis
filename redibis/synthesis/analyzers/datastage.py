"""DataStage textual export analyzer (.dsx / XML).

Executable/binary DataStage packages are rejected at ingest time.
This analyzer extracts stages, links, and source/target identifiers from
textual DSX/XML exports only.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from redibis.synthesis.analyzers.base import register_analyzer
from redibis.synthesis.evidence import (
    EvidenceBundle,
    EvidenceKind,
    EvidenceRecord,
    EvidenceSpan,
)
from redibis.synthesis.ingest import IngestedFile

# Common DSX identifier patterns (text export).
_DSX_IDENTIFIER = re.compile(
    r"(?im)^\s*(?:Identifier|Name)\s+[\"']?([A-Za-z0-9_\.\-]+)[\"']?"
)
_DSX_STAGE = re.compile(
    r"(?is)BEGIN\s+(?:DSJOB|DSSERVER|STAGE)\b.*?Identifier\s+[\"']?([A-Za-z0-9_\.\-]+)"
)
_DSX_LINK = re.compile(
    r"(?im)^\s*LinkName\s+[\"']?([A-Za-z0-9_\.\-]+)[\"']?"
)
_TABLE_HINT = re.compile(
    r"(?i)\b(?:table|tablename|source|target)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\.\-]+)"
)


class DataStageAnalyzer:
    name = "datastage"

    def supports(self, file: IngestedFile) -> bool:
        return file.kind == "datastage" or file.relpath.lower().endswith((".dsx", ".xml"))

    def analyze(self, file: IngestedFile, bundle: EvidenceBundle) -> None:
        text = file.text or ""
        if not text.strip():
            return
        if file.relpath.lower().endswith(".xml") or text.lstrip().startswith("<"):
            self._analyze_xml(file, text, bundle)
        else:
            self._analyze_dsx(file, text, bundle)

    def _analyze_dsx(self, file: IngestedFile, text: str, bundle: EvidenceBundle) -> None:
        for m in _DSX_STAGE.finditer(text):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            bundle.add(EvidenceRecord(
                id=f"ds:stage:{file.relpath}:{name}:{line}",
                kind=EvidenceKind.CUSTOM,
                summary=f"DataStage stage {name}",
                confidence=0.7,
                payload={"stage": name},
                spans=[EvidenceSpan(
                    path=file.relpath, start_line=line, end_line=line, extractor=self.name,
                )],
                source="datastage",
            ))
        for m in _DSX_LINK.finditer(text):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            bundle.add(EvidenceRecord(
                id=f"ds:link:{file.relpath}:{name}:{line}",
                kind=EvidenceKind.JOIN,
                summary=f"DataStage link {name}",
                confidence=0.6,
                payload={"link": name},
                spans=[EvidenceSpan(
                    path=file.relpath, start_line=line, end_line=line, extractor=self.name,
                )],
                source="datastage",
            ))
        for m in _TABLE_HINT.finditer(text):
            table = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            # Heuristic: "target" → write, else read.
            context = text[max(0, m.start() - 40):m.start()].lower()
            kind = EvidenceKind.TABLE_WRITE if "target" in context else EvidenceKind.TABLE_READ
            bundle.add(EvidenceRecord(
                id=f"ds:table:{kind.value}:{file.relpath}:{table}:{line}",
                kind=kind,
                summary=f"{kind.value} {table}",
                confidence=0.65,
                payload={"table": table},
                spans=[EvidenceSpan(
                    path=file.relpath, start_line=line, end_line=line, extractor=self.name,
                )],
                source="datastage",
            ))

    def _analyze_xml(self, file: IngestedFile, text: str, bundle: EvidenceBundle) -> None:
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            bundle.unresolved.append(EvidenceRecord(
                id=f"ds:xml-error:{file.relpath}",
                kind=EvidenceKind.UNRESOLVED,
                summary=f"XML parse failed: {exc}",
                confidence=0.0,
                spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                source="datastage",
            ))
            self._analyze_dsx(file, text, bundle)
            return

        def _local(tag: str) -> str:
            return re.sub(r"^\{.*\}", "", tag).lower()

        def _prop_map(el: ET.Element) -> dict[str, str]:
            props: dict[str, str] = {}
            for child in el.iter():
                if _local(child.tag) != "property":
                    continue
                key = (
                    child.attrib.get("Name")
                    or child.attrib.get("name")
                    or ""
                ).strip()
                val = (child.text or "").strip()
                if key and val:
                    props[key] = val
            return props

        for el in root.iter():
            tag = _local(el.tag)
            attrs = {k.lower(): v for k, v in el.attrib.items()}
            type_attr = str(attrs.get("type") or attrs.get("stagetype") or "")
            ident = (
                el.attrib.get("Identifier")
                or el.attrib.get("Name")
                or el.attrib.get("name")
                or ""
            ).strip()

            # DSX XML exports often use <Record Type="…Stage|Link"> + Property children.
            if tag == "record" and type_attr:
                props = _prop_map(el)
                label = props.get("Name") or ident or type_attr
                if "stage" in type_attr.lower():
                    bundle.add(EvidenceRecord(
                        id=f"ds:xml:stage:{file.relpath}:{label}",
                        kind=EvidenceKind.CUSTOM,
                        summary=f"DataStage XML stage {label}",
                        confidence=0.75,
                        payload={"stage": label, "type": type_attr, "properties": props},
                        spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                        source="datastage",
                    ))
                if "link" in type_attr.lower():
                    bundle.add(EvidenceRecord(
                        id=f"ds:xml:link:{file.relpath}:{label}",
                        kind=EvidenceKind.JOIN,
                        summary=f"DataStage XML link {label}",
                        confidence=0.7,
                        payload={"link": label, "type": type_attr, "properties": props},
                        spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                        source="datastage",
                    ))
                table = props.get("Table") or props.get("table") or props.get("TableName")
                if table:
                    name_l = (props.get("Name") or "").lower()
                    kind = (
                        EvidenceKind.TABLE_WRITE
                        if ("write" in name_l or "target" in name_l)
                        else EvidenceKind.TABLE_READ
                    )
                    bundle.add(EvidenceRecord(
                        id=f"ds:xml:table:{kind.value}:{file.relpath}:{table}",
                        kind=kind,
                        summary=f"{kind.value} {table}",
                        confidence=0.8,
                        payload={"table": table, "stage": label},
                        spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                        source="datastage",
                    ))
                continue

            name = ident or (el.text or "").strip()
            if not name or len(name) > 200:
                continue
            if "stage" in tag:
                bundle.add(EvidenceRecord(
                    id=f"ds:xml:stage:{file.relpath}:{name}",
                    kind=EvidenceKind.CUSTOM,
                    summary=f"DataStage XML stage {name}",
                    confidence=0.7,
                    payload={"stage": name, "tag": tag},
                    spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                    source="datastage",
                ))
            if "link" in tag:
                bundle.add(EvidenceRecord(
                    id=f"ds:xml:link:{file.relpath}:{name}",
                    kind=EvidenceKind.JOIN,
                    summary=f"DataStage XML link {name}",
                    confidence=0.65,
                    payload={"link": name, "tag": tag},
                    spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                    source="datastage",
                ))
            if tag in ("table", "tablename", "source", "target") or "table" in tag:
                kind = EvidenceKind.TABLE_WRITE if "target" in tag else EvidenceKind.TABLE_READ
                bundle.add(EvidenceRecord(
                    id=f"ds:xml:table:{kind.value}:{file.relpath}:{name}",
                    kind=kind,
                    summary=f"{kind.value} {name}",
                    confidence=0.7,
                    payload={"table": name, "tag": tag},
                    spans=[EvidenceSpan(path=file.relpath, extractor=self.name)],
                    source="datastage",
                ))

        # Fallback: plain-text table hints inside the XML dump.
        if not bundle.records:
            self._analyze_dsx(file, text, bundle)


register_analyzer(DataStageAnalyzer())
