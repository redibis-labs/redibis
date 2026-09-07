"""
S3-compatible storage wrapper.
Targets MinIO and SeaweedFS — both expose S3 API via boto3.

Two stores:
  RUN_OUTPUTS_BUCKET  — per-run artifacts (immutable, append-only)
                        layout: {workflow}/{table}/{run_id}/...
  CONTRACTS_BUCKET    — ODCS contract store (smart upsert via service)
                        layout: active/{db}.{table}.yaml
                                audit/{db}.{table}/{uuid}.yaml
                                business/{db}.{table}.yaml
                                merged/{db}.{table}.yaml

Connection note:
  MinIO       endpoint http://localhost:9000  signature_version='s3v4'
  SeaweedFS   endpoint http://localhost:8333  signature_version='s3v4'
  AWS S3      endpoint=None  (uses default)
"""

from __future__ import annotations

import io
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import yaml


# ─────────────────────────────────────────────────────────────────────────────
# Utility: sanitize numpy/pandas scalars for YAML/JSON serialization
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_for_yaml(obj: Any) -> Any:
    """
    Recursively convert numpy/pandas scalars to native Python types.

    Prevents !!python/object/apply:numpy.core.multiarray.scalar tags in YAML
    output. Also handles pandas Timestamp, Categorical, and NA values.

    Applied automatically by StorageBackend.put_yaml() and put_json() —
    callers don't need to sanitize manually.
    """
    if isinstance(obj, dict):
        return {_sanitize_for_yaml(k): _sanitize_for_yaml(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_yaml(v) for v in obj]
    # numpy scalars: int64, float64, bool_, etc. — all have .item()
    if hasattr(obj, "item") and callable(getattr(obj, "item")):
        try:
            return obj.item()
        except (ValueError, TypeError):
            pass
    # pandas Timestamp → ISO string
    if hasattr(obj, "isoformat") and callable(getattr(obj, "isoformat")):
        return obj.isoformat()
    # pandas NA / NaT → None
    try:
        import pandas as pd
        if pd.isna(obj):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    return obj

try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class S3Config:
    """
    Connection config for MinIO / SeaweedFS / AWS S3.

    Set endpoint_url to your MinIO/SeaweedFS server (None → AWS).
    Credentials from explicit args, env vars, or instance profile (in order).
    """
    endpoint_url:        Optional[str] = None    # None = AWS S3
    region_name:         str           = "us-east-1"
    aws_access_key_id:   Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    runs_bucket:         str           = "pii-reports"
    # v2: the active (merged) contract store. One object per schema.table.
    contracts_bucket:    str           = "active-contracts"
    # v2: two dedicated run buckets — one subcontract object per scan run.
    pii_runs_bucket:     str           = "pii-contracts"
    quality_runs_bucket: str           = "quality-contracts"
    pii_configs_bucket:  str           = "pii-configs"
    quality_configs_bucket: str        = "quality-configs"
    # Operational contract telemetry (provenance, pii_summary). When unset,
    # ContractStore uses _meta/telemetry/ inside contracts_bucket.
    metadata_bucket:       Optional[str] = None
    use_ssl:             bool          = False    # True for production endpoints
    signature_version:   str           = "s3v4"   # MinIO / SeaweedFS standard

    @classmethod
    def from_env(cls) -> "S3Config":
        """Build config from standard environment variables."""
        return cls(
            endpoint_url          = os.getenv("S3_ENDPOINT_URL"),
            region_name           = os.getenv("S3_REGION", "us-east-1"),
            aws_access_key_id     = os.getenv("S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key = os.getenv("S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY"),
            runs_bucket           = os.getenv("S3_RUNS_BUCKET",      "pii-reports"),
            contracts_bucket      = os.getenv("S3_CONTRACTS_BUCKET", "active-contracts"),
            pii_runs_bucket       = os.getenv("S3_PII_RUNS_BUCKET",     "pii-contracts"),
            quality_runs_bucket   = os.getenv("S3_QUALITY_RUNS_BUCKET", "quality-contracts"),
            pii_configs_bucket    = os.getenv("S3_PII_CONFIGS_BUCKET", "pii-configs"),
            quality_configs_bucket= os.getenv("S3_QUALITY_CONFIGS_BUCKET", "quality-configs"),
            metadata_bucket       = os.getenv("S3_METADATA_BUCKET"),
            use_ssl               = os.getenv("S3_USE_SSL", "false").lower() == "true",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Storage backend interface (unified across local + S3)
# ─────────────────────────────────────────────────────────────────────────────

class StorageBackend:
    """
    Unified interface for S3-style operations.
    Two implementations: S3Backend (production) and LocalBackend (dev/testing).

    All methods are bucket-aware so the caller passes which bucket to write to.
    """

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Upload bytes. Returns the key."""
        raise NotImplementedError

    def put_text(self, bucket: str, key: str, text: str, content_type: str = "text/plain") -> str:
        """Upload text. Returns the key."""
        return self.put_bytes(bucket, key, text.encode("utf-8"), content_type)

    def put_yaml(self, bucket: str, key: str, obj: Any) -> str:
        """Serialize a Python object to YAML and upload.

        Sanitizes numpy scalars → native Python types before serialization
        and uses yaml.safe_dump to guarantee standard YAML output (no
        !!python/object tags).
        """
        clean = _sanitize_for_yaml(obj)
        text = yaml.safe_dump(
            clean,
            default_flow_style = False,
            sort_keys          = False,
            allow_unicode      = True,
        )
        return self.put_text(bucket, key, text, content_type="application/x-yaml")

    def put_json(self, bucket: str, key: str, obj: Any, indent: int = 2) -> str:
        """Serialize to JSON and upload."""
        clean = _sanitize_for_yaml(obj)
        text = json.dumps(clean, indent=indent, ensure_ascii=False, default=str)
        return self.put_text(bucket, key, text, content_type="application/json")

    def put_file(self, bucket: str, key: str, local_path: Union[str, Path],
                 content_type: str = "application/octet-stream") -> str:
        """Upload a local file."""
        with open(local_path, "rb") as f:
            return self.put_bytes(bucket, key, f.read(), content_type)

    def put_folder(self, bucket: str, key_prefix: str, local_dir: Union[str, Path]) -> list[str]:
        """
        Upload an entire local directory tree to a bucket prefix.
        Used for the GE Data Docs folder (Option B copy).
        Returns the list of uploaded keys.
        """
        local_dir = Path(local_dir)
        if not local_dir.exists() or not local_dir.is_dir():
            raise FileNotFoundError(f"Folder not found: {local_dir}")

        uploaded = []
        for path in local_dir.rglob("*"):
            if path.is_file():
                relative = path.relative_to(local_dir).as_posix()
                key = f"{key_prefix.rstrip('/')}/{relative}"
                content_type = self._guess_content_type(path.suffix)
                self.put_file(bucket, key, path, content_type=content_type)
                uploaded.append(key)
        return uploaded

    def get_bytes(self, bucket: str, key: str) -> bytes:
        """Download as bytes. Raises KeyError if not found."""
        raise NotImplementedError

    def get_text(self, bucket: str, key: str) -> str:
        return self.get_bytes(bucket, key).decode("utf-8")

    def get_yaml(self, bucket: str, key: str) -> Any:
        """Read YAML from storage.

        Handles legacy files that contain !!python/object/apply:numpy tags
        by falling back to yaml.UnsafeLoader (requires numpy installed),
        sanitizing the result, and re-writing the clean version.
        """
        import logging
        log = logging.getLogger(__name__)

        text = self.get_text(bucket, key)
        if not text or not str(text).strip():
            raise ValueError(f"empty or unreadable YAML at {bucket}/{key}")

        try:
            return yaml.safe_load(text)
        except yaml.constructor.ConstructorError:
            # Legacy file with numpy tags — need UnsafeLoader to parse them
            log.warning(
                f"Auto-repairing numpy-corrupted YAML: {bucket}/{key}"
            )
            try:
                raw = yaml.load(text, Loader=yaml.UnsafeLoader)
            except Exception:
                # If UnsafeLoader also fails (numpy not installed),
                # strip the tags with regex and retry safe_load
                import re
                stripped = re.sub(
                    r'!!python/object/apply:numpy\.\S+\s*\n'
                    r'(?:\s+-\s+!!python/object/apply:numpy\.\S+\s*\n)*'
                    r'(?:\s+-\s+.*\n)*',
                    'null\n',
                    text,
                )
                raw = yaml.safe_load(stripped)

            clean = _sanitize_for_yaml(raw)
            # Re-write so this auto-repair only happens once
            try:
                self.put_yaml(bucket, key, clean)
            except Exception:
                pass
            return clean
        except yaml.YAMLError as exc:
            raise ValueError(
                f"corrupt YAML at {bucket}/{key}: {exc}"
            ) from exc

    def get_json(self, bucket: str, key: str) -> Any:
        return json.loads(self.get_text(bucket, key))

    def exists(self, bucket: str, key: str) -> bool:
        """Returns True if key exists."""
        raise NotImplementedError

    def ping(self) -> bool:
        """Cheap reachability check for health/readiness probes."""
        raise NotImplementedError

    def list_keys(self, bucket: str, prefix: str = "", delimiter: str = "") -> list[str]:
        """List keys in bucket under optional prefix. delimiter='/' for folder-like listing."""
        raise NotImplementedError

    def delete(self, bucket: str, key: str) -> None:
        """Delete a single key. No error if not found."""
        raise NotImplementedError

    def delete_prefix(self, bucket: str, prefix: str) -> list[str]:
        """
        Delete every key under a prefix. Returns the list of deleted keys.

        Default implementation (list + delete) works for every backend.
        Used by purge (wipe a table's audit/business/enrichment) and by
        run-bucket clearing.
        """
        deleted: list[str] = []
        for key in self.list_keys(bucket, prefix=prefix):
            self.delete(bucket, key)
            deleted.append(key)
        return deleted

    @staticmethod
    def _guess_content_type(suffix: str) -> str:
        return {
            ".html": "text/html",
            ".css":  "text/css",
            ".js":   "application/javascript",
            ".json": "application/json",
            ".yaml": "application/x-yaml",
            ".yml":  "application/x-yaml",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg":  "image/svg+xml",
            ".csv":  "text/csv",
            ".txt":  "text/plain",
            ".log":  "text/plain",
            ".parquet": "application/octet-stream",
            ".jsonl": "application/x-ndjson",
        }.get(suffix.lower(), "application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# S3 implementation (boto3 + MinIO/SeaweedFS endpoint override)
# ─────────────────────────────────────────────────────────────────────────────

class S3Backend(StorageBackend):
    """
    boto3-based S3 backend. Works against AWS S3, MinIO, and SeaweedFS.
    Auto-creates buckets on first write if they don't exist.
    """

    def __init__(self, config: S3Config):
        if not _BOTO3_AVAILABLE:
            raise ImportError(
                "boto3 is required for S3Backend. "
                "Install with: pip install boto3"
            )
        self.config = config
        self.client = boto3.client(
            "s3",
            endpoint_url          = config.endpoint_url,
            region_name           = config.region_name,
            aws_access_key_id     = config.aws_access_key_id,
            aws_secret_access_key = config.aws_secret_access_key,
            use_ssl               = config.use_ssl,
            config                = Config(signature_version=config.signature_version),
        )
        self._known_buckets: set[str] = set()

    def _ensure_bucket(self, bucket: str) -> None:
        """Create bucket if it doesn't exist (idempotent)."""
        if bucket in self._known_buckets:
            return
        try:
            self.client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket", "403", "Forbidden"):
                try:
                    self.client.create_bucket(Bucket=bucket)
                except ClientError as create_exc:
                    # Bucket might already exist (race condition or 403 was a false alarm)
                    create_code = create_exc.response.get("Error", {}).get("Code", "")
                    if create_code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        raise
            else:
                raise
        self._known_buckets.add(bucket)

    def ping(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.config.contracts_bucket)
            return True
        except Exception:
            return False

    def put_bytes(self, bucket: str, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> str:
        self._ensure_bucket(bucket)
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        body_len = len(data)
        try:
            self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ContentLength=body_len,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code != "IncompleteBody":
                raise
            # MinIO / some S3 proxies reject bare bytes — retry with BytesIO.
            self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=io.BytesIO(data),
                ContentType=content_type,
                ContentLength=body_len,
            )
        return key

    def get_bytes(self, bucket: str, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise KeyError(f"s3://{bucket}/{key} not found")
            raise

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            # 404 = key doesn't exist, 403 = bucket policy issue or bucket doesn't exist
            if code in ("404", "NoSuchKey", "NotFound", "403", "Forbidden"):
                return False
            raise

    def list_keys(self, bucket: str, prefix: str = "", delimiter: str = "") -> list[str]:
        self._ensure_bucket(bucket)
        keys = []
        paginator = self.client.get_paginator("list_objects_v2")
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if delimiter:
            kwargs["Delimiter"] = delimiter
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []) or []:
                keys.append(obj["Key"])
        return keys

    def delete(self, bucket: str, key: str) -> None:
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
        except ClientError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Local filesystem implementation (dev / testing)
# ─────────────────────────────────────────────────────────────────────────────

class LocalBackend(StorageBackend):
    """
    Local filesystem backend mirroring S3 semantics.
    Buckets become subdirectories of a root path.
    Use during development to run the full pipeline without S3 infrastructure.
    """

    def __init__(self, root: Union[str, Path] = "./_local_storage"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _full_path(self, bucket: str, key: str) -> Path:
        return self.root / bucket / key

    def ping(self) -> bool:
        try:
            return self.root.exists()
        except Exception:
            return False

    def put_bytes(self, bucket: str, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> str:
        path = self._full_path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get_bytes(self, bucket: str, key: str) -> bytes:
        path = self._full_path(bucket, key)
        if not path.exists():
            raise KeyError(f"local://{bucket}/{key} not found")
        return path.read_bytes()

    def exists(self, bucket: str, key: str) -> bool:
        return self._full_path(bucket, key).exists()

    def list_keys(self, bucket: str, prefix: str = "", delimiter: str = "") -> list[str]:
        bucket_root = self.root / bucket
        if not bucket_root.exists():
            return []
        keys = []
        for path in bucket_root.rglob("*"):
            if path.is_file():
                relative = path.relative_to(bucket_root).as_posix()
                if relative.startswith(prefix):
                    keys.append(relative)
        return sorted(keys)

    def delete(self, bucket: str, key: str) -> None:
        path = self._full_path(bucket, key)
        if path.exists():
            path.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# Backend factory
# ─────────────────────────────────────────────────────────────────────────────

def get_backend(
    mode: str             = "auto",
    s3_config: Optional[S3Config] = None,
    local_root: Union[str, Path]  = "./_local_storage",
) -> StorageBackend:
    """
    Build a storage backend.

    Args:
        mode : 'auto' (S3 if S3_ENDPOINT_URL env var set, else local)
               | 's3' (force S3)
               | 'local' (force local FS)
        s3_config  : S3Config or None (loads from env)
        local_root : Root directory for local backend
    """
    if mode == "local":
        return LocalBackend(local_root)
    if mode == "s3":
        return S3Backend(s3_config or S3Config.from_env())
    if mode == "auto":
        cfg = s3_config or S3Config.from_env()
        if cfg.endpoint_url or cfg.aws_access_key_id:
            return S3Backend(cfg)
        return LocalBackend(local_root)
    raise ValueError(f"Unknown mode: {mode!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Run output writer — Workflow A and B per-run artifacts
# ─────────────────────────────────────────────────────────────────────────────

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