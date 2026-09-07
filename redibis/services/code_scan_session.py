"""
redibis.services.code_scan_session
====================================
Programmatic scan sessions for Python and CLI — same ``session_id`` / ``run_id``
layout as the web dashboard, with autoflush of reports and contracts.

Layout (under ``output_root``, default ``./scan_output``)::

    {session_id}/
        data.csv
        session.json
        runs/
            {run_id}/
                run_manifest.json
                artifacts/
                    interactive_review.html
                    quality_contract.yaml
                    pii_contract.yaml
                    ...

Contracts are written to the type buckets (``pii-contracts`` / ``quality-contracts``)
via ``ScanService``. When ``auto_write=True`` (CLI/code only), ``automerge`` is
applied so ``ContractStore.upsert()`` merges into the active contract immediately.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union, TYPE_CHECKING

import pandas as pd

from redibis.scan_mode import parse_scan_mode
from redibis.services.scan_service import ScanConfig, ScanResult, ScanService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import StorageBackend
from redibis.store.subcontract_store import SubcontractStore

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from redibis.config import RedibisConfig


def _new_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"scan_{ts}_{uuid.uuid4().hex[:6]}"


def _resolve_automerge(*, auto_write: bool, automerge: str) -> str:
    if auto_write:
        import warnings
        warnings.warn(
            "auto_write is deprecated; subcontracts are always written. "
            "Pass automerge='pii'|'quality'|'both' to merge into active.",
            DeprecationWarning,
            stacklevel=2,
        )
    return (automerge or "none").lower()


@dataclass
class CodeScanSession:
    """
    A durable scan session backed by on-disk layout compatible with the web UI.

    Usage::

        session = CodeScanSession.create(df, table="telecom.customers")
        result = session.scan(auto_write=True)
        print(result.session_id, result.run_id, session.artifacts_dir)
    """

    session_id: str
    table: str
    output_root: Path
    backend: StorageBackend
    store: ContractStore
    runs_bucket: str = "pii-reports"
    sub_store: Optional[SubcontractStore] = None
    _data_path: Optional[Path] = field(default=None, repr=False)
    _runs: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def session_dir(self) -> Path:
        return self.output_root / self.session_id

    @property
    def artifacts_dir(self) -> Optional[Path]:
        """Artifacts directory for the most recent run, if any."""
        if not self._runs:
            return None
        run_id = self._runs[-1]["run_id"]
        return self.session_dir / "runs" / run_id / "artifacts"

    @classmethod
    def create(
        cls,
        data: Union[pd.DataFrame, str, Path, bytes],
        *,
        table: str,
        output_root: Union[str, Path] = "./scan_output",
        backend: StorageBackend,
        store: ContractStore,
        runs_bucket: str = "pii-reports",
        sub_store: Optional[SubcontractStore] = None,
        session_id: Optional[str] = None,
        filename: str = "data.csv",
    ) -> "CodeScanSession":
        """Create a new session directory and persist input data."""
        output_root = Path(output_root)
        session_id = session_id or str(uuid.uuid4())
        session_dir = output_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        data_path = session_dir / "data.csv"
        if isinstance(data, pd.DataFrame):
            data.to_csv(data_path, index=False)
        elif isinstance(data, bytes):
            data_path.write_bytes(data)
        elif isinstance(data, (str, Path)):
            path = Path(data)
            if path.suffix.lower() in (".parquet", ".pq"):
                pd.read_parquet(path).to_csv(data_path, index=False)
            else:
                data_path.write_bytes(path.read_bytes())
        else:
            raise TypeError(f"unsupported data type: {type(data)!r}")

        session = cls(
            session_id=session_id,
            table=table,
            output_root=output_root,
            backend=backend,
            store=store,
            runs_bucket=runs_bucket,
            sub_store=sub_store,
            _data_path=data_path,
        )
        session._write_session_index(status="initialized")
        return session

    @classmethod
    def open(
        cls,
        session_id: str,
        *,
        output_root: Union[str, Path] = "./scan_output",
        backend: StorageBackend,
        store: ContractStore,
        runs_bucket: str = "pii-reports",
        sub_store: Optional[SubcontractStore] = None,
    ) -> "CodeScanSession":
        """Re-open an existing session directory on disk."""
        output_root = Path(output_root)
        session_dir = output_root / session_id
        index_path = session_dir / "session.json"
        if not index_path.exists():
            raise FileNotFoundError(f"session not found: {session_dir}")

        state = json.loads(index_path.read_text(encoding="utf-8"))
        table = state.get("table_name") or state.get("table") or ""
        if not table:
            raise ValueError(f"session {session_id} missing table_name in session.json")

        data_path = session_dir / "data.csv"
        if not data_path.exists():
            raise FileNotFoundError(f"session data missing: {data_path}")

        session = cls(
            session_id=session_id,
            table=table,
            output_root=output_root,
            backend=backend,
            store=store,
            runs_bucket=runs_bucket,
            sub_store=sub_store,
            _data_path=data_path,
            _runs=list(state.get("runs") or []),
        )
        return session

    def scan(
        self,
        *,
        mode: str = "all",
        auto_write: bool = False,
        automerge: str = "none",
        equation_mode: str = "independent",
        run_pii: Optional[bool] = None,
        run_quality: Optional[bool] = None,
        generate_ge_docs: bool = False,
        validate_contracts: bool = True,
        config: Optional[ScanConfig] = None,
        redibis_config: Optional["RedibisConfig"] = None,
    ) -> ScanResult:
        """
        Run a scan on the session data and autoflush reports + contracts.

        Parameters
        ----------
        mode
            ``all`` | ``profile`` | ``pii`` | ``quality`` | comma list — ignored
            when ``run_pii`` / ``run_quality`` are set explicitly or ``config``
            is supplied.
        auto_write
            When True, merge run subcontracts into the active contract immediately
            (``automerge="both"``). Intended for CLI/code only; web stays manual.
        automerge
            ``"none"`` | ``"pii"`` | ``"quality"`` | ``"both"`` when ``auto_write``
            is False.
        config
            Optional full ``ScanConfig``; session/run layout fields are applied
            automatically on top.
        """
        if self._data_path is None or not self._data_path.exists():
            raise RuntimeError("session has no data file")

        run_id = _new_run_id()
        artifacts_dir = self.session_dir / "runs" / run_id / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        if run_pii is None or run_quality is None:
            parsed_pii, parsed_profile, parsed_quality = parse_scan_mode(mode)
            if run_pii is None:
                run_pii = parsed_pii
            if run_quality is None:
                run_quality = parsed_quality
            run_profile = parsed_profile
        else:
            _, run_profile, _ = parse_scan_mode(mode)

        effective_automerge = _resolve_automerge(auto_write=auto_write, automerge=automerge)

        base = config or ScanConfig(table=self.table)
        scan_config = replace(
            base,
            table=self.table,
            session_id=self.session_id,
            run_id=run_id,
            artifacts_dir=artifacts_dir,
            output_dir=self.session_dir,
            run_pii=run_pii,
            run_profile=run_profile,
            run_quality=run_quality,
            equation_mode=equation_mode if config is None else base.equation_mode,
            generate_ge_docs=generate_ge_docs if config is None else base.generate_ge_docs,
            validate_contracts=validate_contracts if config is None else base.validate_contracts,
            automerge=effective_automerge,
        )

        df = pd.read_csv(self._data_path, dtype=str, keep_default_na=False)
        service = ScanService(
            backend=self.backend,
            store=self.store,
            runs_bucket=self.runs_bucket,
            sub_store=self.sub_store,
        )
        result = service.scan_dataframe(df, scan_config, redibis_config=redibis_config)
        self._flush_run_manifest(run_id, result, artifacts_dir)
        self._runs.append({
            "run_id": run_id,
            "status": result.status,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "artifacts_dir": str(artifacts_dir.relative_to(self.session_dir)),
            "quality_contract_version": result.quality_contract_version,
            "pii_contract_version": result.pii_contract_version,
        })
        self._write_session_index(status="scan_complete" if result.status == "success" else result.status)
        return result

    def _flush_run_manifest(
        self,
        run_id: str,
        result: ScanResult,
        artifacts_dir: Path,
    ) -> Path:
        """Write ``runs/{run_id}/run_manifest.json`` with relative artifact paths."""
        rel_artifacts = {}
        for key, path in result.artifacts.items():
            if key.startswith("s3:"):
                rel_artifacts[key] = path
                continue
            p = Path(path)
            try:
                rel_artifacts[key] = str(p.relative_to(artifacts_dir))
            except ValueError:
                rel_artifacts[key] = path

        manifest = {
            "session_id": self.session_id,
            "run_id": run_id,
            "table": result.table,
            "status": result.status,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "duration_seconds": result.duration_seconds,
            "artifacts_dir": "artifacts",
            "artifacts": rel_artifacts,
            "quality_contract_version": result.quality_contract_version,
            "pii_contract_version": result.pii_contract_version,
            "counts": {
                "total_rows": result.total_rows,
                "total_columns": result.total_columns,
                "quality_passed": result.quality_passed,
                "quality_expectations": result.quality_expectations,
                "pii_columns_detected": result.pii_columns_detected,
                "pii_columns_scanned": result.pii_columns_scanned,
            },
        }
        run_dir = self.session_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = run_dir / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        return manifest_path

    def _write_session_index(self, *, status: str) -> None:
        payload = {
            "session_id": self.session_id,
            "table_name": self.table,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "output_root": str(self.output_root),
            "data_path": "data.csv",
            "runs": self._runs,
            "latest_run_id": self._runs[-1]["run_id"] if self._runs else None,
        }
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "session.json").write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
