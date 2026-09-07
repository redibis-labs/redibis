"""Contract Synthesis — deterministic analyzers."""

from __future__ import annotations

from pathlib import Path

from redibis.synthesis.analyzers import run_analyzers
from redibis.synthesis.evidence import EvidenceBundle, EvidenceKind
from redibis.synthesis.ingest import ingest_paths

CAFC = Path(__file__).resolve().parents[1] / "fixtures" / "synthesis" / "cafc"


def _bundle_for(*paths: Path) -> EvidenceBundle:
    manifest = ingest_paths(paths)
    assert not manifest.errors, manifest.errors
    bundle = EvidenceBundle()
    run_analyzers(manifest.files, bundle)
    return bundle


def test_requirements_extract_ids():
    bundle = _bundle_for(CAFC / "requirements" / "01_pipeline_requirements.md")
    ids = {rid for row in bundle.traceability for rid in [row.requirement_id]}
    assert "BR-01" in ids
    assert "DQ-01" in ids
    assert "AC-01" in ids
    assert any(r.kind == EvidenceKind.REQUIREMENT for r in bundle.records)


def test_sql_reads_writes_and_joins():
    bundle = _bundle_for(CAFC / "sources" / "cafc_build.sql")
    tables_read = {
        (r.payload or {}).get("table")
        for r in bundle.by_kind(EvidenceKind.TABLE_READ)
    }
    tables_write = {
        (r.payload or {}).get("table")
        for r in bundle.by_kind(EvidenceKind.TABLE_WRITE)
    }
    assert "crm.customers" in tables_read or any("customers" in str(t) for t in tables_read)
    assert any("customer_feature_composite_daily" in str(t) for t in tables_write)
    assert bundle.by_kind(EvidenceKind.JOIN) or bundle.by_kind(EvidenceKind.COLUMN_TRANSFORM)


def test_spark_ast_only_no_import(tmp_path: Path):
    # Malicious file that would fail if executed/imported.
    evil = tmp_path / "boom.py"
    evil.write_text(
        "raise SystemExit('should not execute')\n"
        "spark.table('crm.customers')\n"
        "df.write.saveAsTable('analytics.customer_feature_composite_daily')\n",
        encoding="utf-8",
    )
    bundle = _bundle_for(evil)
    assert bundle.by_kind(EvidenceKind.TABLE_READ) or bundle.by_kind(EvidenceKind.TABLE_WRITE)


def test_datastage_stages_and_links():
    bundle = _bundle_for(CAFC / "sources" / "cafc_job.dsx")
    assert bundle.records
    kinds = {r.kind for r in bundle.records}
    assert EvidenceKind.TABLE_READ in kinds or EvidenceKind.TABLE_WRITE in kinds or EvidenceKind.CUSTOM in kinds or EvidenceKind.JOIN in kinds
