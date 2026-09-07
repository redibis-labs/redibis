"""Contract Synthesis — ingest security (ZIP traversal, types, no execution)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from redibis.synthesis.ingest import ingest_bytes_map, ingest_paths


def test_ingest_rejects_binary_nul(tmp_path: Path):
    bad = tmp_path / "payload.bin"
    # Use allowed suffix but binary content.
    bad = tmp_path / "payload.sql"
    bad.write_bytes(b"SELECT 1;\x00DROP")
    m = ingest_paths([bad])
    assert m.errors
    assert any("binary" in e.lower() for e in m.errors)


def test_ingest_zip_traversal_blocked():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.sql", "SELECT 1")
        zf.writestr("/abs.sql", "SELECT 1")
    m = ingest_bytes_map({"pack.zip": buf.getvalue()})
    assert m.errors
    assert any("unsafe" in e.lower() for e in m.errors)


def test_ingest_accepts_sql_and_requirements(tmp_path: Path):
    req = tmp_path / "REQ-demo.md"
    req.write_text("**BR-01 Purpose.** Demo requirement.\n", encoding="utf-8")
    sql = tmp_path / "job.sql"
    sql.write_text("SELECT 1 AS x FROM crm.customers;\n", encoding="utf-8")
    m = ingest_paths([req, sql])
    assert not m.errors, m.errors
    kinds = {f.kind for f in m.files}
    assert "requirements" in kinds
    assert "sql" in kinds
    assert all(f.sha256 for f in m.files)


def test_ingest_directory_expands_allowed_files():
    root = Path(__file__).resolve().parents[1] / "fixtures" / "synthesis" / "cafc"
    m = ingest_paths([root / "sources"])
    assert not m.errors, m.errors
    assert len(m.files) >= 3
    assert {f.kind for f in m.files} >= {"sql", "spark", "datastage"}


def test_ingest_rejects_executable_package(tmp_path: Path):
    jar = tmp_path / "job.jar"
    jar.write_bytes(b"PK\x03\x04not-a-real-jar")
    m = ingest_paths([tmp_path])
    assert any("rejected" in e.lower() for e in m.errors)


def test_secret_pattern_warning_and_key_redaction(tmp_path: Path):
    src = tmp_path / "cfg.py"
    src.write_text(
        "password = hunter2\n"
        "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    m = ingest_paths([src])
    assert not m.errors, m.errors
    assert m.files
    assert "REDACTED PRIVATE KEY" in m.files[0].text
    assert any("secret" in w.lower() or "private key" in w.lower() for w in m.files[0].warnings)
