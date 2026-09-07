"""Source analyzer protocol for Contract Synthesis."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from redibis.synthesis.evidence import EvidenceBundle
from redibis.synthesis.ingest import IngestedFile


@runtime_checkable
class SourceAnalyzer(Protocol):
    name: str

    def supports(self, file: IngestedFile) -> bool:
        ...

    def analyze(self, file: IngestedFile, bundle: EvidenceBundle) -> None:
        ...


ANALYZER_REGISTRY: dict[str, SourceAnalyzer] = {}


def register_analyzer(analyzer: SourceAnalyzer) -> SourceAnalyzer:
    ANALYZER_REGISTRY[analyzer.name] = analyzer
    return analyzer


def list_analyzers() -> list[SourceAnalyzer]:
    return list(ANALYZER_REGISTRY.values())


def run_analyzers(files: list[IngestedFile], bundle: EvidenceBundle) -> EvidenceBundle:
    for file in files:
        matched = False
        for analyzer in list_analyzers():
            if analyzer.supports(file):
                analyzer.analyze(file, bundle)
                matched = True
        if not matched and file.kind not in ("requirements",):
            from redibis.synthesis.evidence import EvidenceKind, EvidenceRecord, EvidenceSpan

            bundle.unresolved.append(EvidenceRecord(
                id=f"unparsed:{file.relpath}",
                kind=EvidenceKind.UNRESOLVED,
                summary=f"No analyzer claimed {file.relpath} ({file.kind})",
                confidence=0.0,
                spans=[EvidenceSpan(path=file.relpath, extractor="registry")],
                source=file.kind,
            ))
    return bundle
