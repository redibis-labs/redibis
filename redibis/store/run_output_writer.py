"""
redibis.store.run_output_writer
================================
RunOutputWriter — writes per-run artifacts to the runs bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from redibis.store.storage_backend import StorageBackend

#: Durable, shareable evidence copies live outside the per-run workflow prefix.
EVIDENCE_META_PREFIX = "_meta/evidence"


def evidence_storage_key(table: str, run_id: str) -> str:
    """Storage key for a stripped (or masked) evidence bundle.

    Session-scoped writers may carry ``{session_id}/{run_id}`` as the workflow
    run id. Evidence lives under a **bare** run id; session scope is metadata.
    """
    table_id = (table or "").strip()
    rid = bare_evidence_run_id(run_id)
    if not table_id or not rid:
        raise ValueError("table and run_id are required for evidence storage")
    if ".." in table_id or table_id.startswith("/") or "\\" in table_id:
        raise ValueError(f"invalid evidence table id: {table!r}")
    if "/" in rid or "\\" in rid or ".." in rid:
        raise ValueError(f"invalid evidence run_id: {run_id!r}")
    return f"{EVIDENCE_META_PREFIX}/{table_id}/{rid}.json"


def bare_evidence_run_id(run_id: str) -> str:
    """Last path segment of a possibly session-prefixed run id."""
    rid = (run_id or "").strip().replace("\\", "/")
    return rid.rsplit("/", 1)[-1].strip()


@dataclass
class RunOutputWriter:
    """
    Writes per-run artifacts to the runs bucket.

    Layout:
        {workflow}/{table_safe}/{run_id}/
            run_manifest.json
            ge_report/                 (folder from Option B copy)
            data_contract.yaml         (workflow's partial contract)
            triage_report.html
            pii_summary.csv
            pii_detections.json
            sample_preview.parquet
            logs/
                pipeline.log
                tool_calls.jsonl
    """
    backend:  StorageBackend
    bucket:   str
    workflow: str            # 'ge' | 'pii' | 'business' | 'merge'
    table:    str            # 'telecom.customers'
    run_id:   str            # '2026-05-09_14-32-18'

    @property
    def prefix(self) -> str:
        table_safe = self.table.replace(".", "_")
        return f"{self.workflow}/{table_safe}/{self.run_id}"

    def write(self, name: str, content: Any, content_type: str = "auto") -> str:
        """
        Write a named artifact. Auto-detects format from extension if content_type=='auto'.
        Returns the full S3 key.
        """
        key = f"{self.prefix}/{name}"
        if content_type == "auto":
            if name.endswith((".yaml", ".yml")):
                self.backend.put_yaml(self.bucket, key, content)
            elif name.endswith(".json") or name.endswith(".jsonl"):
                if isinstance(content, str):
                    self.backend.put_text(self.bucket, key, content,
                                          content_type="application/json")
                else:
                    self.backend.put_json(self.bucket, key, content)
            elif isinstance(content, bytes):
                self.backend.put_bytes(self.bucket, key, content)
            elif isinstance(content, str):
                self.backend.put_text(self.bucket, key, content,
                                      content_type=self.backend._guess_content_type(
                                          Path(name).suffix
                                      ))
            else:
                self.backend.put_yaml(self.bucket, key, content)
        elif content_type == "yaml":
            self.backend.put_yaml(self.bucket, key, content)
        elif content_type == "json":
            self.backend.put_json(self.bucket, key, content)
        else:
            data = content if isinstance(content, bytes) else str(content).encode("utf-8")
            self.backend.put_bytes(self.bucket, key, data, content_type=content_type)
        return key

    def write_file(self, name: str, local_path: Union[str, Path]) -> str:
        """Upload a local file under this run's prefix."""
        key = f"{self.prefix}/{name}"
        content_type = self.backend._guess_content_type(Path(name).suffix)
        return self.backend.put_file(self.bucket, key, local_path, content_type=content_type)

    def write_folder(self, name: str, local_dir: Union[str, Path]) -> list[str]:
        """Upload a local folder tree under this run's prefix."""
        prefix = f"{self.prefix}/{name}"
        return self.backend.put_folder(self.bucket, prefix, local_dir)

    def write_evidence(self, content: Any) -> str:
        """Persist evidence under ``_meta/evidence/{table}/{run_id}.json``.

        This path is outside the per-run workflow prefix so the stored copy is
        the durable, shareable one (PLAN §5.2). Still the only writer to the
        runs bucket (invariant 7).
        """
        key = evidence_storage_key(self.table, self.run_id)
        if isinstance(content, str):
            self.backend.put_text(
                self.bucket, key, content, content_type="application/json",
            )
        else:
            self.backend.put_json(self.bucket, key, content)
        return key


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    print("── s3_storage.py smoke test (LocalBackend) ────────────────")

    with tempfile.TemporaryDirectory() as td:
        backend = LocalBackend(td)

        # Put + get bytes
        backend.put_bytes("test-bucket", "hello.txt", b"hello world")
        assert backend.exists("test-bucket", "hello.txt")
        assert backend.get_bytes("test-bucket", "hello.txt") == b"hello world"
        print("✓ put_bytes / get_bytes / exists")

        # Put + get YAML
        backend.put_yaml("test-bucket", "config.yaml",
                         {"name": "demo", "tags": ["pii", "regulated"]})
        loaded = backend.get_yaml("test-bucket", "config.yaml")
        assert loaded["name"] == "demo"
        print("✓ put_yaml / get_yaml")

        # List keys
        backend.put_text("test-bucket", "active/telecom.customers.yaml", "stub")
        backend.put_text("test-bucket", "active/eshop.orders.yaml",      "stub")
        backend.put_text("test-bucket", "audit/telecom.customers/u1.yaml", "stub")
        active = backend.list_keys("test-bucket", prefix="active/")
        assert len(active) == 2
        assert "active/telecom.customers.yaml" in active
        print("✓ list_keys with prefix")

        # Folder upload
        src = Path(td) / "src_folder"
        (src / "subdir").mkdir(parents=True)
        (src / "index.html").write_text("<html></html>")
        (src / "subdir" / "style.css").write_text("body{}")
        keys = backend.put_folder("test-bucket", "uploaded/", src)
        assert len(keys) == 2
        assert any(k.endswith("index.html") for k in keys)
        assert any(k.endswith("style.css") for k in keys)
        print("✓ put_folder uploads recursive tree")

        # RunOutputWriter
        writer = RunOutputWriter(
            backend=backend, bucket="test-runs",
            workflow="pii", table="telecom.customers",
            run_id="2026-05-09_14-32-18",
        )
        writer.write("data_contract.yaml", {"apiVersion": "v3.0.1"})
        writer.write("pii_summary.csv", "col,verdict\nphone,PII")
        assert backend.exists("test-runs",
                              "pii/telecom_customers/2026-05-09_14-32-18/data_contract.yaml")
        assert backend.exists("test-runs",
                              "pii/telecom_customers/2026-05-09_14-32-18/pii_summary.csv")
        print("✓ RunOutputWriter writes under correct prefix")

    print("\n✅ All s3_storage smoke tests passed")
