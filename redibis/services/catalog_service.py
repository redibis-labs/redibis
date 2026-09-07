"""
CatalogService — push active ODCS contracts to data-governance catalogs.

Backend-neutral orchestration: loads contracts from ``ContractStore``,
delegates to a concrete ``CatalogPublisher`` (OpenMetadata today), records
telemetry in ``ContractMetadataStore``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from redibis.config import CatalogConfig, RedibisConfig
from redibis.services.catalog.base import CatalogPushOptions, CatalogPushResult, CatalogStatus
from redibis.services.catalog.registry import available_backends, get_publisher
from redibis.store.catalog_ledger import (
    CatalogAuditStore,
    CatalogLedgerStore,
    EntityResolutionCache,
    SuppressionStore,
)
from redibis.store.contract_store import ContractStore


@dataclass
class CatalogService:
    store: ContractStore
    config: CatalogConfig
    runs_bucket: str = "pii-reports"
    # Optional RedibisConfig (for classification.enabled / policy pack).
    redibis_config: Optional[RedibisConfig] = None
    ledger_store: Optional[CatalogLedgerStore] = None
    suppression_store: Optional[SuppressionStore] = None
    entity_cache: Optional[EntityResolutionCache] = None
    audit_store: Optional[CatalogAuditStore] = None

    def __post_init__(self) -> None:
        # Wire catalog overlays under ``_meta/`` from the contracts bucket.
        backend = self.store.backend
        bucket = self.store.bucket
        if self.ledger_store is None:
            self.ledger_store = CatalogLedgerStore(backend, bucket)
        if self.suppression_store is None:
            self.suppression_store = SuppressionStore(backend, bucket)
        if self.entity_cache is None:
            self.entity_cache = EntityResolutionCache(backend, bucket)
        if self.audit_store is None:
            self.audit_store = CatalogAuditStore(backend, bucket)

    @classmethod
    def from_redibis_config(cls, store: ContractStore, cfg: RedibisConfig) -> "CatalogService":
        return cls(
            store=store,
            config=cfg.catalog,
            runs_bucket=cfg.storage.runs_bucket,
            redibis_config=cfg,
        )

    def _publisher(self, backend: Optional[str] = None) -> Any:
        cfg = self._config_for_backend(backend)
        publisher = get_publisher(cfg)
        attach = getattr(publisher, "attach_stores", None)
        if callable(attach):
            attach(
                ledger_store=self.ledger_store,
                suppression_store=self.suppression_store,
                entity_cache=self.entity_cache,
                audit_store=self.audit_store,
            )
        return publisher

    def _push_options(
        self,
        *,
        lifecycle_artifacts: Optional[dict] = None,
        classification_results: Optional[list] = None,
        reconcile_mode: str = "normal",
        clear_suppressions: bool = False,
        enforce_reason: str = "",
        enforce_actor: str = "",
        column_telemetry: Optional[dict] = None,
        metadata_store: Any = None,
        scan_coverage: Optional[list] = None,
        quality_results: Optional[list] = None,
        refresh_entity: bool = False,
    ) -> CatalogPushOptions:
        p = self.config.push
        return CatalogPushOptions(
            tags=p.tags,
            glossary=p.glossary,
            contract=p.contract,
            quality=p.quality,
            masked_samples=p.masked_samples,
            lifecycle_diff=p.lifecycle_diff,
            lifecycle_artifacts=lifecycle_artifacts,
            classification_results=classification_results,
            reconcile_mode=reconcile_mode,
            clear_suppressions=clear_suppressions,
            enforce_reason=enforce_reason,
            enforce_actor=enforce_actor,
            column_telemetry=column_telemetry,
            metadata_store=metadata_store,
            scan_coverage=scan_coverage,
            quality_results=quality_results,
            refresh_entity=refresh_entity,
        )

    def _classify_if_enabled(self, contract: dict, table: str) -> Optional[list]:
        """Run deterministic policy classification when enabled; else None."""
        cfg = self.redibis_config
        if cfg is None or not getattr(cfg, "classification", None):
            return None
        if not cfg.classification.enabled:
            return None
        from redibis.classification import ClassificationService, JurisdictionContext, get_builtin_pack

        pack_name = cfg.classification.policy_pack or "telecom"
        svc = ClassificationService(get_builtin_pack(pack_name))
        jurisdiction = (cfg.classification.default_jurisdiction or "").strip()
        if jurisdiction:
            svc.set_jurisdiction(JurisdictionContext(table=table, jurisdiction=jurisdiction))
        return svc.classify_contract(
            contract,
            table,
            use_memory=bool(getattr(cfg.memory, "enabled", False)),
        )

    def push(
        self,
        table: str,
        *,
        dry_run: bool = False,
        backend: Optional[str] = None,
        reconcile_mode: str = "normal",
        clear_suppressions: bool = False,
        enforce_reason: str = "",
        enforce_actor: str = "",
        refresh_entity: bool = False,
    ) -> CatalogPushResult:
        contract = self.store.get_active(table)
        if contract is None:
            raise ValueError(f"No active contract for {table!r}")
        lifecycle_artifacts = None
        if self.config.push.lifecycle_diff:
            from redibis.services.catalog.lifecycle_artifacts import load_latest_lifecycle_artifacts

            lifecycle_artifacts = load_latest_lifecycle_artifacts(
                self.store.backend, self.runs_bucket, table,
            )
        scan_coverage = None
        quality_results = None
        try:
            from redibis.services.pipeline import load_latest_scan_coverage

            scan_coverage = load_latest_scan_coverage(
                self.store.backend, self.runs_bucket, table,
            )
        except Exception:  # noqa: BLE001 — coverage is optional; fall back to contract-derived
            scan_coverage = None
        try:
            from redibis.services.continuous_quality import load_latest_quality_results

            q_payload = load_latest_quality_results(
                self.store.backend, self.runs_bucket, table,
            )
            if isinstance(q_payload, dict):
                quality_results = q_payload.get("results")
        except Exception:  # noqa: BLE001
            quality_results = None
        return self.push_contract(
            contract, table, dry_run=dry_run, backend=backend,
            lifecycle_artifacts=lifecycle_artifacts,
            scan_coverage=scan_coverage,
            quality_results=quality_results,
            reconcile_mode=reconcile_mode,
            clear_suppressions=clear_suppressions,
            enforce_reason=enforce_reason,
            enforce_actor=enforce_actor,
            refresh_entity=refresh_entity,
        )

    def push_contract(
        self,
        contract: dict,
        table: str,
        *,
        dry_run: bool = False,
        backend: Optional[str] = None,
        lifecycle_artifacts: Optional[dict] = None,
        classification_results: Optional[list] = None,
        reconcile_mode: str = "normal",
        clear_suppressions: bool = False,
        enforce_reason: str = "",
        enforce_actor: str = "",
        refresh_entity: bool = False,
        scan_coverage: Optional[list] = None,
        quality_results: Optional[list] = None,
    ) -> CatalogPushResult:
        if classification_results is None:
            classification_results = self._classify_if_enabled(contract, table)
        column_telemetry = None
        try:
            column_telemetry = self.store.metadata.get_column_telemetry(table) or None
        except Exception:  # noqa: BLE001
            column_telemetry = None
        publisher = self._publisher(backend)
        result = publisher.push(
            contract, table,
            options=self._push_options(
                lifecycle_artifacts=lifecycle_artifacts,
                classification_results=classification_results,
                reconcile_mode=reconcile_mode,
                clear_suppressions=clear_suppressions,
                enforce_reason=enforce_reason,
                enforce_actor=enforce_actor,
                column_telemetry=column_telemetry,
                metadata_store=self.store.metadata,
                scan_coverage=scan_coverage,
                quality_results=quality_results,
                refresh_entity=refresh_entity,
            ),
            dry_run=dry_run,
        )

        if not dry_run and self.store.get_active(table) is not None:
            self.store.metadata.record_catalog_push(
                table,
                backend=result.backend,
                entity_fqn=result.entity_fqn,
                contract_version=contract.get("version", ""),
                details={
                    "entity_id": result.entity_id,
                    "contract_id": result.contract_id,
                    "glossary_count": result.glossary_count,
                    "policy_tags": (result.details or {}).get("policy_tags") or [],
                },
            )
        return result

    def push_file(
        self,
        path: Union[str, Path],
        *,
        table: Optional[str] = None,
        dry_run: bool = False,
        backend: Optional[str] = None,
    ) -> CatalogPushResult:
        from redibis.services.catalog.mapping import load_contract_file, resolve_contract_table

        contract = load_contract_file(path)
        resolved = resolve_contract_table(contract, override=table, path=path)
        return self.push_contract(
            contract, resolved, dry_run=dry_run, backend=backend,
        )

    def push_batch(
        self,
        root: Union[str, Path],
        *,
        table: Optional[str] = None,
        glob: Optional[str] = None,
        recursive: bool = False,
        dry_run: bool = False,
        backend: Optional[str] = None,
        fail_fast: bool = False,
    ) -> tuple[list[CatalogPushResult], list[tuple[str, str]]]:
        from redibis.services.catalog.mapping import iter_contract_files

        results: list[CatalogPushResult] = []
        errors: list[tuple[str, str]] = []
        for path in iter_contract_files(root, glob=glob, recursive=recursive):
            try:
                results.append(
                    self.push_file(
                        path, table=table, dry_run=dry_run, backend=backend,
                    )
                )
            except Exception as exc:
                errors.append((str(path), str(exc)))
                if fail_fast:
                    break
        return results, errors

    def push_all(
        self,
        *,
        dry_run: bool = False,
        backend: Optional[str] = None,
        fail_fast: bool = False,
    ) -> tuple[list[CatalogPushResult], list[tuple[str, str]]]:
        """Push every active contract. Isolates failures unless ``fail_fast``."""
        results: list[CatalogPushResult] = []
        errors: list[tuple[str, str]] = []
        for table in self.store.list_tables():
            try:
                results.append(self.push(table, dry_run=dry_run, backend=backend))
            except Exception as exc:
                errors.append((table, str(exc)))
                if fail_fast:
                    break
        return results, errors

    def push_scan(
        self,
        scan_dir: Union[str, Path],
        *,
        database: str = "",
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        tables_file: Optional[Union[str, Path]] = None,
        dry_run: bool = False,
        backend: Optional[str] = None,
        fail_fast: bool = False,
        skip_in_sync: bool = False,
        resume: Optional[str] = None,
        retry_failed: bool = True,
    ) -> dict[str, Any]:
        """
        Push active contracts for tables discovered in a scan-output folder.

        Returns a report dict with ``run``, ``results``, ``errors``, and paths.
        Failures are isolated per table unless ``fail_fast`` is set.
        """
        from datetime import datetime, timezone
        import logging

        from redibis.services.catalog.push_run import CatalogPushRunStore
        from redibis.services.catalog.scan_tables import filter_tables, tables_from_scan_dir

        log = logging.getLogger(__name__)
        scan_path = Path(scan_dir)
        store = CatalogPushRunStore(scan_path)
        backend_name = (backend or self.config.backend or "openmetadata").lower()

        if resume:
            run = store.load(resume)
            # Always persist progress under the run's canonical scan_dir.
            canonical = Path(run.scan_dir).resolve() if run.scan_dir else scan_path.resolve()
            if scan_path.resolve() != canonical:
                store = CatalogPushRunStore(canonical)
                scan_path = canonical
            if backend and backend.lower() != (run.backend or "").lower():
                raise ValueError(
                    f"resume backend mismatch: run={run.backend!r} cli={backend!r}"
                )
            if dry_run and not run.dry_run:
                raise ValueError(
                    "Refusing --dry-run on a non-dry-run push run; it would mark "
                    "tasks succeeded without pushing. Resume without --dry-run."
                )
            backend_name = (run.backend or backend_name).lower()
            dry_run = bool(run.dry_run)
            active_versions = {
                t: (self.store.get_active(t) or {}).get("version", "")
                for t in [task.table for task in run.tasks]
            }
            todo = store.resolve_resume(
                run, active_versions=active_versions, retry_failed=retry_failed,
            )
            # Persist version-change re-queues immediately.
            store.save(run)
        else:
            discovered = tables_from_scan_dir(scan_path)
            selected = filter_tables(
                discovered,
                database=database,
                include=include,
                exclude=exclude,
                tables_file=tables_file,
            )
            active_set = set(self.store.list_tables())
            versions: dict[str, str] = {}
            excluded: dict[str, str] = {}
            skip_in_sync_check_failures: dict[str, str] = {}
            for table in selected:
                if table not in active_set:
                    excluded[table] = "no active contract in store"
                    continue
                contract = self.store.get_active(table) or {}
                versions[table] = str(contract.get("version") or "")
                if skip_in_sync:
                    try:
                        st = self.status(table, backend=backend_name)
                        if st.in_sync is True:
                            excluded[table] = "already in sync with catalog"
                    except Exception as exc:
                        # Fail-safe: push anyway (do not silently skip a table we
                        # could not verify), but surface the check failure in the
                        # manifest so operators can see --skip-in-sync degraded.
                        msg = str(exc)
                        skip_in_sync_check_failures[table] = msg
                        log.warning(
                            "skip-in-sync status check failed for %s (%s); will push",
                            table, exc,
                        )
            run = store.create(
                selected,
                backend=backend_name,
                dry_run=dry_run,
                filters={
                    "database": database,
                    "include": include or [],
                    "exclude": exclude or [],
                    "tables_file": str(tables_file) if tables_file else "",
                    "skip_in_sync": skip_in_sync,
                    "skip_in_sync_check_failures": skip_in_sync_check_failures,
                },
                versions=versions,
                excluded=excluded,
            )
            todo = [
                t.table for t in run.tasks
                if t.status == "pending"
            ]

        results: list[CatalogPushResult] = []
        errors: list[tuple[str, str]] = []
        task_map = run.task_map()

        for table in todo:
            task = task_map.get(table)
            if task is None:
                continue
            task.status = "running"
            task.started_at = datetime.now(timezone.utc).isoformat()
            task.attempts += 1
            task.error = ""
            store.save(run)
            try:
                result = self.push(table, dry_run=dry_run, backend=backend_name)
                results.append(result)
                task.status = "succeeded"
                task.entity_fqn = result.entity_fqn
                task.entity_id = result.entity_id
                task.contract_id = result.contract_id
                task.contract_version = (
                    (self.store.get_active(table) or {}).get("version", "")
                    or task.contract_version
                )
                task.details = {
                    "glossary_count": result.glossary_count,
                    **(result.details or {}),
                }
                if not run.om_version and (result.details or {}).get("om_version"):
                    run.om_version = str(result.details["om_version"])
                if not run.contract_strategy and (result.details or {}).get("contract_strategy"):
                    run.contract_strategy = str(result.details["contract_strategy"])
                task.finished_at = datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                msg = str(exc)
                errors.append((table, msg))
                task.status = "failed"
                task.error = msg
                task.finished_at = datetime.now(timezone.utc).isoformat()
                if fail_fast:
                    store.save(run)
                    break
            store.save(run)

        run.finished_at = datetime.now(timezone.utc).isoformat()
        manifest_path = store.save(run)
        return {
            "run": run.to_dict(),
            "run_id": run.run_id,
            "manifest": str(manifest_path),
            "run_dir": str(store.run_dir(run.run_id)),
            "results": results,
            "errors": errors,
            "summary": run.summary(),
        }

    def status(self, table: str, *, backend: Optional[str] = None) -> CatalogStatus:
        backend_name = (backend or self.config.backend or "openmetadata").lower()
        contract = self.store.get_active(table)
        if contract is None:
            raise ValueError(f"No active contract for {table!r}")

        telemetry = self.store.metadata.get_telemetry(table)
        catalog = (telemetry.get("catalog") or {}).get(backend_name)
        version = contract.get("version", "")
        in_sync: Optional[bool] = None
        if catalog:
            pushed_version = catalog.get("contract_version", "")
            in_sync = pushed_version == version if pushed_version else None

        return CatalogStatus(
            table=table,
            backend=backend_name,
            contract_version=version,
            last_push=catalog,
            in_sync=in_sync,
        )

    def list_backends(self) -> list[str]:
        return available_backends()

    def ledger_entries(self, table: str, *, backend: Optional[str] = None) -> list:
        backend_name = (backend or self.config.backend or "openmetadata").lower()
        assert self.ledger_store is not None
        return self.ledger_store.get_entries(table, backend_name)

    def list_suppressions(self, table: str, *, backend: Optional[str] = None) -> list:
        backend_name = (backend or self.config.backend or "openmetadata").lower()
        assert self.suppression_store is not None
        return self.suppression_store.list(table, backend_name)

    def add_suppression(
        self,
        table: str,
        *,
        column_path: str,
        facet: str,
        value_key: str,
        actor: str,
        reason: str,
        asset_fqn: str = "",
        backend: Optional[str] = None,
    ) -> Any:
        from redibis.services.catalog.assertions import AssertionKey, Facet, Suppression

        backend_name = (backend or self.config.backend or "openmetadata").lower()
        assert self.suppression_store is not None
        fqn = asset_fqn or table
        key = AssertionKey(
            asset_fqn=fqn,
            column_path=column_path,
            facet=Facet(facet),
            value_key=value_key,
        )
        suppression = Suppression(
            key=key,
            actor=actor,
            reason=reason,
            created_at=datetime.now(timezone.utc),
            source="cli",
        )
        self.suppression_store.add(table, backend_name, suppression)
        return suppression

    def remove_suppression(
        self,
        table: str,
        *,
        column_path: str,
        facet: str,
        value_key: str,
        asset_fqn: str = "",
        backend: Optional[str] = None,
    ) -> bool:
        from redibis.services.catalog.assertions import AssertionKey, Facet

        backend_name = (backend or self.config.backend or "openmetadata").lower()
        assert self.suppression_store is not None
        key = AssertionKey(
            asset_fqn=asset_fqn or table,
            column_path=column_path,
            facet=Facet(facet),
            value_key=value_key,
        )
        return self.suppression_store.remove(table, backend_name, key)

    def sync_feedback(self, *, backend: Optional[str] = None) -> dict:
        """Poll OM change events and write suppressions for steward tag removals."""
        backend_name = (backend or self.config.backend or "openmetadata").lower()
        if backend_name != "openmetadata":
            raise ValueError("feedback sync is only supported for openmetadata")
        publisher = self._publisher(backend_name)
        from redibis.services.catalog.feedback import sync_feedback
        from redibis.services.catalog.openmetadata import _OpenMetadataClient

        settings = getattr(publisher, "settings", None)
        if settings is None:
            raise ValueError("OpenMetadata publisher settings unavailable")
        client = _OpenMetadataClient.from_settings(settings)
        assert self.suppression_store is not None
        assert self.ledger_store is not None
        return sync_feedback(
            client,
            backend=backend_name,
            storage=self.store.backend,
            bucket=self.store.bucket,
            suppression_store=self.suppression_store,
            ledger_store=self.ledger_store,
        )

    def delete_tables(
        self,
        tables: Optional[list[str]] = None,
        *,
        fqns: Optional[list[str]] = None,
        dry_run: bool = True,
        hard_delete: bool = True,
        backend: Optional[str] = None,
    ) -> dict:
        """Delete OpenMetadata tables by ``schema.table`` and/or raw FQN."""
        backend_name = (backend or self.config.backend or "openmetadata").lower()
        if backend_name != "openmetadata":
            raise ValueError("catalog delete is only supported for openmetadata")
        publisher = self._publisher(backend_name)
        delete = getattr(publisher, "delete_tables", None)
        if not callable(delete):
            raise ValueError("OpenMetadata publisher does not support delete_tables")
        return delete(
            tables=tables,
            fqns=fqns,
            dry_run=dry_run,
            hard_delete=hard_delete,
        )

    def wipe_catalog(
        self,
        *,
        dry_run: bool = True,
        hard_delete: bool = True,
        with_glossary: bool = False,
        with_classifications: bool = False,
        service_name: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> dict:
        """Wipe the configured OpenMetadata database service tree."""
        backend_name = (backend or self.config.backend or "openmetadata").lower()
        if backend_name != "openmetadata":
            raise ValueError("catalog wipe is only supported for openmetadata")
        publisher = self._publisher(backend_name)
        wipe = getattr(publisher, "wipe", None)
        if not callable(wipe):
            raise ValueError("OpenMetadata publisher does not support wipe")
        return wipe(
            dry_run=dry_run,
            hard_delete=hard_delete,
            with_glossary=with_glossary,
            with_classifications=with_classifications,
            service_name=service_name,
        )

    def _config_for_backend(self, backend: Optional[str]) -> CatalogConfig:
        if backend is None or backend == self.config.backend:
            return self.config
        from dataclasses import replace
        return replace(self.config, backend=backend)
