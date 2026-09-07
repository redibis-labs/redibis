"""OpenMetadata catalog publisher (concrete backend)."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

from redibis.config import CatalogConfig, ConfigError
from redibis.services.catalog.assertions import (
    Assertion,
    AssertionKey,
    CoverageRecord,
    Facet,
    LedgerEntry,
    build_assertions_from_contract,
    value_hash,
)
from redibis.services.catalog.base import (
    CatalogPublisher,
    CatalogPushOptions,
    CatalogPushResult,
)
from redibis.services.catalog.mapping import (
    ColumnSemantics,
    column_catalog_tag_names,
    iter_columns,
    redibis_glossary_name,
    schema_object,
    split_physical_name,
    table_semantics,
)
from redibis.services.catalog.openmetadata_patch import (
    build_json_patch,
    remote_assertions_from_table,
)
from redibis.services.catalog.openmetadata_resolve import resolve_table_fqn
from redibis.services.catalog.reconcile import (
    ReconcileMode,
    ReconcilePolicy,
    IntentOp,
    reconcile,
    render_reconcile_plan,
)

_LOGICAL_TO_OM: dict[str, str] = {
    "string": "VARCHAR",
    "str": "VARCHAR",
    "text": "VARCHAR",
    "integer": "INT",
    "int": "INT",
    "long": "BIGINT",
    "float": "FLOAT",
    "double": "DOUBLE",
    "decimal": "DECIMAL",
    "number": "DECIMAL",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "timestamp": "TIMESTAMP",
}


class OpenMetadataConflictError(RuntimeError):
    """HTTP 409 from OpenMetadata (version / concurrent update)."""


def _logical_to_om_type(logical: str, physical: str = "") -> tuple[str, Optional[int]]:
    key = (logical or "string").lower()
    om = _LOGICAL_TO_OM.get(key, "VARCHAR")
    length: Optional[int] = None
    if om == "VARCHAR":
        m = re.search(r"\((\d+)\)", physical or "")
        if m:
            length = int(m.group(1))
        elif key in ("string", "str", "text"):
            length = 256
    return om, length


def _is_already_exists_error(exc: BaseException) -> bool:
    """True only for 409 or an explicit already-exists body — never bare 400."""
    msg = str(exc)
    if re.search(r"\b409\b", msg):
        return True
    lower = msg.lower()
    if "already exists" in lower or "entity already exists" in lower:
        return True
    return False


def _om_column_tags(
    col: ColumnSemantics,
    *,
    include_tags: bool,
    confidence: float = 1.0,
    confirm_threshold: float = 0.85,
) -> list[dict]:
    """OM TagLabel wrappers around ``column_catalog_tag_names`` (R14 + R1)."""
    state = "Confirmed" if confidence >= confirm_threshold else "Suggested"
    return [
        {
            "tagFQN": name,
            "source": "Classification",
            "labelType": "Automated",
            "state": state,
        }
        for name in column_catalog_tag_names(col, include_tags=include_tags)
    ]


def _tag_setup_from_fqns(tag_fqns: set[str]) -> list[dict]:
    """Build classification/tag PUT steps for Redibis / RedibisPolicy FQNs."""
    by_classification: dict[str, dict[str, str]] = {}
    for fqn in tag_fqns:
        if fqn.startswith("RedibisPolicy."):
            by_classification.setdefault("RedibisPolicy", {})[fqn] = fqn.split(".", 1)[1]
        elif fqn.startswith("Redibis."):
            by_classification.setdefault("Redibis", {})[fqn] = fqn.split(".", 1)[1]
    if not by_classification:
        return []

    descriptions = {
        "RedibisPolicy": "Deterministic redibis policy-engine classifications",
        "Redibis": "Redibis entity-type and contract tags from ODCS contracts",
    }
    steps: list[dict] = []
    for classification, needed in sorted(by_classification.items()):
        steps.append({
            "target": "classification",
            "method": "PUT",
            "path": "/v1/classifications",
            "body": {
                "name": classification,
                "displayName": classification,
                "description": descriptions.get(
                    classification, f"Redibis classification {classification}",
                ),
            },
        })
        for fqn, tag_name in sorted(needed.items()):
            display = tag_name.replace("__", ":")
            steps.append({
                "target": "tag",
                "method": "PUT",
                "path": "/v1/tags",
                "body": {
                    "name": tag_name,
                    "displayName": display,
                    "description": f"{classification} tag {display}",
                    "classification": classification,
                },
            })
    return steps


def _tag_setup_steps(plan: "OpenMetadataPushPlan") -> list[dict]:
    """Ensure classification tags referenced by columns exist before table upsert."""
    fqns: set[str] = set()
    for col in plan.table_payload.get("columns") or []:
        for tag in col.get("tags") or []:
            fqn = tag.get("tagFQN") or ""
            if fqn:
                fqns.add(fqn)
    return _tag_setup_from_fqns(fqns)


def _tag_setup_from_ops(ops: list[IntentOp]) -> list[dict]:
    fqns: set[str] = set()
    for op in ops:
        if op.key.facet is Facet.GLOSSARY_LINK:
            continue  # glossary terms are PUT separately, not classification tags
        if op.op in ("add", "update", "promote", "demote") and op.key.value_key:
            fqns.add(op.key.value_key)
        if op.value and isinstance(op.value, str) and (
            op.value.startswith("Redibis.") or op.value.startswith("RedibisPolicy.")
        ):
            fqns.add(op.value)
    return _tag_setup_from_fqns(fqns)


# Back-compat alias used by older call sites / tests.
_policy_tag_setup_steps = _tag_setup_steps


@dataclass
class OpenMetadataPushPlan:
    table: str
    table_fqn: str
    service_payload: dict
    database_payload: dict
    schema_payload: dict
    table_payload: dict
    glossary_terms: list[dict] = field(default_factory=list)
    odcs_document: dict = field(default_factory=dict)


@dataclass
class OpenMetadataSettings:
    host: str = "http://localhost:8585"
    jwt: str = ""
    service_name: str = "redibis"
    default_schema: str = "default"
    entity_mode: str = "mixed"  # enrich_existing | create_if_missing | mixed
    fqn_map: dict = field(default_factory=dict)
    search_services: list = field(default_factory=list)
    min_version: str = "1.12.4"
    publish_threshold: float = 0.60
    confirm_threshold: float = 0.85
    delete_after_negatives: int = 2
    resolve_cache_ttl_hours: int = 168


def _validate_om_jwt(jwt: str, *, env_name: str = "OM_BOT_JWT") -> str:
    """Reject empty / placeholder JWTs before httpx builds Authorization headers.

    HTTP headers must be ASCII. A copied tutorial placeholder such as ``…``
    otherwise surfaces as a cryptic ``'ascii' codec can't encode character`` error.
    """
    token = (jwt or "").strip()
    if not token:
        raise ConfigError(
            f"OpenMetadata JWT missing — set catalog.openmetadata.jwt_env or {env_name}"
        )
    placeholders = {
        "...", "…", "eyJ...", "eyJ…", "<token>", "<jwt>", "YOUR_TOKEN", "changeme",
    }
    if token in placeholders or all(ch in ".…" for ch in token):
        raise ConfigError(
            f"{env_name} looks like a documentation placeholder ({token!r}), not a real "
            "OpenMetadata bot/admin JWT. Export a real token from OM Settings → Bots "
            "or the /api/v1/users/login response accessToken."
        )
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigError(
            f"{env_name} contains non-ASCII characters and cannot be sent in an "
            "Authorization header. Paste the raw JWT access token (ASCII only), not a "
            "docs placeholder such as an ellipsis."
        ) from exc
    return token


def attach_lifecycle_artifacts_to_odcs(
    odcs_doc: dict,
    artifacts: dict,
) -> dict:
    """Embed governance diff + provenance into ODCS customProperties for OM reviewers (G6)."""
    doc = copy.deepcopy(odcs_doc)
    props = list(doc.get("customProperties") or [])
    if not isinstance(props, list):
        props = []

    def _set(prop: str, value: str) -> None:
        props[:] = [p for p in props if not (
            isinstance(p, dict) and p.get("property") == prop
        )]
        if value:
            props.append({"property": prop, "value": value})

    diff_md = artifacts.get("diff_md") or ""
    if diff_md:
        _set("redibis_contract_diff_md", diff_md[:50000])
    active_ref = artifacts.get("active_ref") or {}
    if active_ref:
        _set(
            "redibis_active_source",
            str(active_ref.get("source") or ""),
        )
        _set(
            "redibis_lifecycle_run_id",
            str(active_ref.get("run_id") or artifacts.get("run_id") or ""),
        )
        if active_ref.get("det_version"):
            _set("redibis_det_audit_version", str(active_ref["det_version"]))

    summary = artifacts.get("diff_json", {}).get("summary") if isinstance(
        artifacts.get("diff_json"), dict,
    ) else None
    if summary:
        _set(
            "redibis_diff_summary",
            f"overrides={summary.get('overrides', 0)}; "
            f"adds={summary.get('adds', 0)}; "
            f"unchanged={summary.get('unchanged', 0)}",
        )

    if props:
        doc["customProperties"] = props
    return doc


def build_openmetadata_plan(
    contract: dict,
    table: str,
    *,
    service_name: str = "redibis",
    default_schema: str = "default",
    options: Optional[CatalogPushOptions] = None,
    confirm_threshold: float = 0.85,
) -> OpenMetadataPushPlan:
    options = options or CatalogPushOptions()
    meta = table_semantics(contract, table)
    database, table_name = split_physical_name(table)

    service_fqn = service_name
    database_fqn = f"{service_fqn}.{database}"
    database_schema_fqn = f"{database_fqn}.{default_schema}"
    table_fqn = f"{database_schema_fqn}.{table_name}"
    glossary_name = redibis_glossary_name(database)

    columns = []
    for col in iter_columns(
        contract, table, classification_results=options.classification_results,
    ):
        data_type, data_length = _logical_to_om_type(col.logical_type, col.physical_type)
        payload: dict[str, Any] = {
            "name": col.name,
            "dataType": data_type,
            "description": col.description,
        }
        if col.business_name:
            payload["displayName"] = col.business_name
        if data_length:
            payload["dataLength"] = data_length
        tags = _om_column_tags(
            col,
            include_tags=options.tags,
            confidence=1.0,
            confirm_threshold=confirm_threshold,
        )
        if tags:
            payload["tags"] = tags
        columns.append(payload)

    glossary: list[dict] = []
    if options.glossary:
        seen: set[str] = set()
        for col in iter_columns(
            contract, table, classification_results=options.classification_results,
        ):
            display = col.business_name or f"{table}.{col.name}"
            if display in seen:
                continue
            seen.add(display)
            glossary.append({
                "name": col.name,
                "displayName": display,
                "description": col.description or f"Column {col.name} on {table}",
                "glossary": glossary_name,
                "synonyms": [display] if display != col.name else [],
                "relatedTerms": [],
                "references": [{"name": table_fqn, "endpoint": f"/table/{table_fqn}"}],
            })

    odcs_doc = copy.deepcopy(contract)
    odcs_doc.setdefault("physicalName", table)
    if options.lifecycle_artifacts:
        odcs_doc = attach_lifecycle_artifacts_to_odcs(
            odcs_doc, options.lifecycle_artifacts,
        )

    table_description = meta.description[:500]
    if options.lifecycle_artifacts and options.lifecycle_artifacts.get("diff_md"):
        table_description = (
            f"{table_description}\n\n"
            "[redibis] LLM enrichment diff attached in ODCS customProperties "
            "(redibis_contract_diff_md)."
        )[:2000]

    return OpenMetadataPushPlan(
        table=table,
        table_fqn=table_fqn,
        service_payload={
            "name": service_name,
            "serviceType": "CustomDatabase",
            "description": "Redibis-managed ODCS contracts (agent catalog)",
        },
        database_payload={
            "name": database,
            "service": service_fqn,
            "description": f"Database {database} (from redibis physicalName)",
        },
        schema_payload={
            "name": default_schema,
            "database": database_fqn,
            "description": f"Default schema for {database}",
        },
        table_payload={
            "name": table_name,
            "databaseSchema": database_schema_fqn,
            "displayName": meta.schema_name,
            "description": table_description,
            "tableType": "Regular",
            "columns": columns,
        },
        glossary_terms=glossary,
        odcs_document=odcs_doc,
    )


def plan_to_preview(plan: OpenMetadataPushPlan, options: CatalogPushOptions) -> dict[str, Any]:
    steps: list[dict[str, Any]] = list(_policy_tag_setup_steps(plan))
    steps.extend([
        {"target": "databaseService", "method": "PUT", "path": "/v1/services/databaseServices",
         "body": plan.service_payload},
        {"target": "database", "method": "PUT", "path": "/v1/databases", "body": plan.database_payload},
        {"target": "databaseSchema", "method": "PUT", "path": "/v1/databaseSchemas",
         "body": plan.schema_payload},
        {"target": "table", "method": "PUT", "path": "/v1/tables", "body": plan.table_payload},
    ])
    if options.glossary and plan.glossary_terms:
        steps.append({
            "target": "glossaryTerms", "method": "PUT", "path": "/v1/glossaryTerms",
            "body": plan.glossary_terms,
        })
    if options.contract:
        steps.append({
            "target": "dataContract", "method": "PUT", "path": "/v1/dataContracts/odcs",
            "query": {"entityType": "table", "mode": "replace"},
            "body_format": "odcs_json", "body": plan.odcs_document,
        })
    policy_fqns = sorted({
        t.get("tagFQN")
        for col in (plan.table_payload.get("columns") or [])
        for t in (col.get("tags") or [])
        if (t.get("tagFQN") or "").startswith("RedibisPolicy.")
    })
    return {
        "backend": "openmetadata",
        "table_fqn": plan.table_fqn,
        "steps": steps,
        "policy_tags": policy_fqns,
    }


def openmetadata_ui_url(host: str, table_fqn: str) -> str:
    """Browser URL for a table (and its ODCS data contract tab) in OpenMetadata UI."""
    base = host.rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/table/{table_fqn}"


def _parse_version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in re.split(r"[^\d]+", version or ""):
        if token.isdigit():
            parts.append(int(token))
    return tuple(parts) if parts else (0,)


def _reconcile_policy_from_settings(settings: OpenMetadataSettings) -> ReconcilePolicy:
    return ReconcilePolicy(
        publish_threshold=float(settings.publish_threshold),
        confirm_threshold=float(settings.confirm_threshold),
        delete_after_negatives=int(settings.delete_after_negatives),
    )


def _ledger_updates_from_ops(
    ops: list[IntentOp],
    *,
    backend: str,
    scan_id: str = "",
) -> tuple[list[LedgerEntry], list[AssertionKey]]:
    """Return (upserts, removals) for ledger after a successful write."""
    now = datetime.now(timezone.utc)
    upserts: list[LedgerEntry] = []
    removals: list[AssertionKey] = []
    for op in ops:
        if op.op == "remove":
            removals.append(op.key)
            continue
        if op.op in ("add", "update", "promote", "demote"):
            upserts.append(
                LedgerEntry(
                    backend=backend,
                    key=op.key,
                    value_hash=value_hash(op.value),
                    scan_id=scan_id,
                    published_at=now,
                    state=op.state or "confirmed",
                    negative_streak=1 if op.op == "demote" else 0,
                )
            )
    return upserts, removals


def _glossary_term_payload_from_op(op: IntentOp, *, table_fqn: str) -> dict:
    """Build an OM glossary-term body from a ``GLOSSARY_LINK`` IntentOp."""
    term_fqn = (op.key.value_key or "").strip()
    if "." in term_fqn:
        glossary_name, term_name = term_fqn.rsplit(".", 1)
    else:
        glossary_name = "Redibis"
        term_name = op.key.column_path or term_fqn or "term"
    display = str(op.value or term_name)
    return {
        "name": term_name,
        "displayName": display,
        "description": (
            f"Column {op.key.column_path}" if op.key.column_path else display
        ),
        "glossary": glossary_name,
        "synonyms": [display] if display != term_name else [],
        "relatedTerms": [],
        "references": [{"name": table_fqn, "endpoint": f"/table/{table_fqn}"}],
    }

def parse_om_version(version_payload: Any) -> tuple[int, int, int]:
    """Parse OpenMetadata version string/payload into ``(major, minor, patch)``."""
    raw = version_payload
    if isinstance(version_payload, dict):
        raw = (
            version_payload.get("version")
            or version_payload.get("versionInfo")
            or version_payload.get("revision")
            or ""
        )
        if isinstance(raw, dict):
            raw = raw.get("version") or raw.get("revision") or ""
    text = str(raw or "").strip()
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def uses_odcs_data_contract_api(version: tuple[int, int, int]) -> bool:
    """ODCS import endpoint is supported on OpenMetadata 1.12+.

    Unparseable versions ``(0, 0, 0)`` raise so callers do not silently pick the
    wrong Data Contract strategy.
    """
    if version == (0, 0, 0):
        raise ValueError(
            "Could not parse OpenMetadata server version; refusing to choose "
            "Data Contract API strategy"
        )
    return version >= (1, 12, 0)


def map_odcs_status_to_entity_status(status: Any) -> str:
    raw = str(status or "active").strip().lower()
    if raw in ("draft",):
        return "Draft"
    if raw in ("deprecated", "retired"):
        return "Deprecated"
    return "Active"


def build_native_data_contract_payload(
    contract: dict,
    table: str,
    *,
    table_id: str,
    table_fqn: str = "",
) -> dict[str, Any]:
    """
    Map an ODCS contract into OpenMetadata ``CreateDataContract`` shape (pre-1.12).

    Uses ``entityStatus`` (not ODCS ``status``) and an OM-shaped schema list.
    """
    database, table_name = split_physical_name(table)
    schema_obj = schema_object(contract, table)

    columns: list[dict[str, Any]] = []
    for prop in schema_obj.get("properties", []) or []:
        if not isinstance(prop, dict) or not prop.get("name"):
            continue
        data_type, data_length = _logical_to_om_type(
            str(prop.get("logicalType") or "string"),
            str(prop.get("physicalType") or ""),
        )
        col: dict[str, Any] = {
            "name": prop["name"],
            "dataType": data_type,
            "description": prop.get("description") or prop.get("businessName") or "",
        }
        if data_length:
            col["dataLength"] = data_length
        columns.append(col)

    name = (
        str(contract.get("name") or "").strip()
        or f"{database}_{table_name}_contract"
    )
    description = (
        (schema_obj.get("description") if isinstance(schema_obj, dict) else None)
        or contract.get("description")
        or f"Data contract for {table}"
    )
    if isinstance(description, dict):
        description = description.get("purpose") or description.get("description") or str(description)

    payload: dict[str, Any] = {
        "name": name,
        "displayName": name,
        "description": str(description)[:2000],
        "entityStatus": map_odcs_status_to_entity_status(contract.get("status")),
        "entity": {
            "id": table_id,
            "type": "table",
        },
        "schema": columns,
    }
    if table_fqn:
        payload["entity"]["fullyQualifiedName"] = table_fqn
    return payload


class _OpenMetadataClient:
    def __init__(self, settings: OpenMetadataSettings, *, timeout: float = 60.0):
        base = settings.host.rstrip("/")
        if not base.endswith("/api"):
            base = f"{base}/api"
        self.base_url = base
        self.jwt = settings.jwt
        self.timeout = timeout
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: OpenMetadataSettings) -> "_OpenMetadataClient":
        env_name = "OM_BOT_JWT"
        jwt = _validate_om_jwt(
            settings.jwt or os.environ.get(env_name, ""),
            env_name=env_name,
        )
        return cls(OpenMetadataSettings(
            host=settings.host,
            jwt=jwt,
            service_name=settings.service_name,
            default_schema=settings.default_schema,
            entity_mode=settings.entity_mode,
            fqn_map=dict(settings.fqn_map or {}),
            search_services=list(settings.search_services or []),
            min_version=settings.min_version,
            publish_threshold=settings.publish_threshold,
            confirm_threshold=settings.confirm_threshold,
            delete_after_negatives=settings.delete_after_negatives,
            resolve_cache_ttl_hours=int(
                getattr(settings, "resolve_cache_ttl_hours", 168) or 168
            ),
        ))

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt}", "Content-Type": content_type}

    def _url(self, path: str, query: Optional[dict] = None) -> str:
        path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        query: Optional[dict] = None,
        content_type: str = "application/json",
        allow_statuses: Optional[set[int]] = None,
    ) -> tuple[int, dict]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for catalog push. Install: pip install 'redibis[catalog]'"
            ) from exc
        url = self._url(path, query)
        headers = self._headers(content_type)
        body = None
        if json_body is not None:
            body = (
                json.dumps(json_body)
                if not isinstance(json_body, (bytes, str))
                else json_body
            )
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.request(method, url, headers=headers, content=body)
        allow = allow_statuses or set()
        if resp.status_code in allow:
            payload = resp.json() if resp.content else {}
            return resp.status_code, payload if isinstance(payload, dict) else {}
        if resp.status_code == 409:
            raise OpenMetadataConflictError(
                f"OpenMetadata {method} {path} (409): {resp.text[:500]}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenMetadata {method} {path} ({resp.status_code}): {resp.text[:500]}"
            )
        return resp.status_code, (resp.json() if resp.content else {})

    def put(self, path: str, body: dict) -> dict:
        _status, data = self._request("PUT", path, json_body=body)
        return data

    def delete(
        self,
        path: str,
        *,
        hard_delete: bool = True,
        recursive: bool = False,
        allow_missing: bool = True,
    ) -> dict[str, Any]:
        """DELETE an OM entity by API path (``/v1/tables/name/...`` etc.)."""
        query = {
            "hardDelete": "true" if hard_delete else "false",
            "recursive": "true" if recursive else "false",
        }
        allow = {404} if allow_missing else set()
        status, data = self._request(
            "DELETE", path, query=query, allow_statuses=allow,
        )
        return {
            "path": path,
            "status": status,
            "hard_delete": hard_delete,
            "recursive": recursive,
            "missing": status == 404,
            "body": data,
        }

    def delete_table_fqn(
        self,
        fqn: str,
        *,
        hard_delete: bool = True,
        recursive: bool = True,
        allow_missing: bool = True,
    ) -> dict[str, Any]:
        encoded = quote(fqn, safe="")
        result = self.delete(
            f"/v1/tables/name/{encoded}",
            hard_delete=hard_delete,
            recursive=recursive,
            allow_missing=allow_missing,
        )
        result["entity_fqn"] = fqn
        result["kind"] = "table"
        return result

    def delete_database_service(
        self,
        service_name: str,
        *,
        hard_delete: bool = True,
        recursive: bool = True,
        allow_missing: bool = True,
    ) -> dict[str, Any]:
        encoded = quote(service_name, safe="")
        result = self.delete(
            f"/v1/services/databaseServices/name/{encoded}",
            hard_delete=hard_delete,
            recursive=recursive,
            allow_missing=allow_missing,
        )
        result["entity_fqn"] = service_name
        result["kind"] = "databaseService"
        return result

    def delete_glossary(
        self,
        name: str = "Redibis",
        *,
        hard_delete: bool = True,
        recursive: bool = True,
        allow_missing: bool = True,
    ) -> dict[str, Any]:
        encoded = quote(name, safe="")
        result = self.delete(
            f"/v1/glossaries/name/{encoded}",
            hard_delete=hard_delete,
            recursive=recursive,
            allow_missing=allow_missing,
        )
        result["entity_fqn"] = name
        result["kind"] = "glossary"
        return result

    def delete_classification(
        self,
        name: str,
        *,
        hard_delete: bool = True,
        allow_missing: bool = True,
    ) -> dict[str, Any]:
        encoded = quote(name, safe="")
        result = self.delete(
            f"/v1/classifications/name/{encoded}",
            hard_delete=hard_delete,
            recursive=False,
            allow_missing=allow_missing,
        )
        result["entity_fqn"] = name
        result["kind"] = "classification"
        return result

    def list_tables_for_service(self, service_name: str, *, limit: int = 100) -> list[dict]:
        """Best-effort list of tables under a database service."""
        out: list[dict] = []
        # Prefer service-scoped listing when supported.
        try:
            status, data = self._request(
                "GET",
                "/v1/tables",
                query={"limit": str(limit), "service": service_name},
                allow_statuses={400, 404},
            )
            if status == 200:
                for row in data.get("data") or []:
                    if isinstance(row, dict):
                        out.append(row)
                if out:
                    return out
        except RuntimeError:
            pass
        # Fallback: search index
        for hit in self.search_tables(service_name, size=limit):
            src = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
            if not isinstance(src, dict):
                continue
            fqn = str(src.get("fullyQualifiedName") or "")
            if fqn.startswith(f"{service_name}."):
                out.append(src)
        return out

    def get_system_version(self) -> dict:
        _status, data = self._request("GET", "/v1/system/version", allow_statuses={404})
        return data if _status == 200 else {}

    def check_min_version(self) -> dict[str, Any]:
        """Compare remote version to ``settings.min_version``. Returns telemetry."""
        info: dict[str, Any] = {
            "min_version": self.settings.min_version,
            "remote_version": "",
            "ok": True,
            "warning": "",
        }
        try:
            ver = self.get_system_version()
        except Exception as exc:  # noqa: BLE001
            info["ok"] = False
            info["warning"] = f"version probe failed: {exc}"
            return info
        remote = str(ver.get("version") or ver.get("revision") or "")
        info["remote_version"] = remote
        if remote and _parse_version_tuple(remote) < _parse_version_tuple(
            self.settings.min_version
        ):
            info["ok"] = False
            info["warning"] = (
                f"OpenMetadata {remote} is below min_version "
                f"{self.settings.min_version}"
            )
        return info

    def get_table(
        self,
        fqn: str,
        *,
        fields: str = "columns,tags,owners",
    ) -> dict:
        encoded = quote(fqn, safe="")
        query = {"fields": fields} if fields else None
        _status, data = self._request("GET", f"/v1/tables/name/{encoded}", query=query)
        return data

    def get_table_or_none(
        self,
        fqn: str,
        *,
        fields: str = "columns,tags",
    ) -> Optional[dict]:
        encoded = quote(fqn, safe="")
        query = {"fields": fields} if fields else None
        try:
            status, data = self._request(
                "GET",
                f"/v1/tables/name/{encoded}",
                query=query,
                allow_statuses={404},
            )
        except RuntimeError:
            return None
        if status == 404:
            return None
        return data

    def search_tables(self, query: str, *, size: int = 50) -> list[dict]:
        try:
            status, data = self._request(
                "GET",
                "/v1/search/query",
                query={
                    "q": query,
                    "index": "table_search_index",
                    "from": "0",
                    "size": str(size),
                },
                allow_statuses={404},
            )
        except RuntimeError:
            return []
        if status == 404:
            return []
        hits = data.get("hits")
        if isinstance(hits, dict):
            return list(hits.get("hits") or [])
        if isinstance(data.get("data"), list):
            return list(data["data"])
        return []

    def patch_table(self, fqn: str, patch: list[dict]) -> dict:
        encoded = quote(fqn, safe="")
        _status, data = self._request(
            "PATCH",
            f"/v1/tables/name/{encoded}",
            json_body=patch,
            content_type="application/json-patch+json",
        )
        return data

    def get_events(
        self,
        *,
        entity_type: str = "table",
        timestamp_ms: int = 0,
    ) -> list[dict]:
        status, data = self._request(
            "GET",
            "/v1/events",
            query={"entityType": entity_type, "timestamp": str(int(timestamp_ms))},
            allow_statuses={404},
        )
        if status == 404:
            return []
        if isinstance(data.get("data"), list):
            return list(data["data"])
        if isinstance(data, list):
            return list(data)
        return []

    def ensure_policy_tags(self, plan: OpenMetadataPushPlan) -> int:
        """Create Redibis / RedibisPolicy classifications + tags (idempotent)."""
        return self.ensure_tag_steps(_tag_setup_steps(plan))

    def ensure_tag_steps(self, steps: list[dict]) -> int:
        if not steps:
            return 0
        created = 0
        for step in steps:
            try:
                self.put(step["path"], step["body"])
                created += 1
            except RuntimeError as exc:
                if _is_already_exists_error(exc):
                    continue
                raise
        return created

    def ensure_glossary(self, name: str = "Redibis") -> dict:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError("httpx is required — pip install 'redibis[catalog]'") from exc
        url = self._url(f"/v1/glossaries/name/{name}")
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, headers=self._headers())
        if resp.status_code == 200 and resp.content:
            return resp.json()
        return self.put("/v1/glossaries", {
            "name": name,
            "displayName": name,
            "description": "Business terms pushed from redibis ODCS contracts",
        })

    def upsert_hierarchy(self, plan: OpenMetadataPushPlan) -> None:
        self.put("/v1/services/databaseServices", plan.service_payload)
        self.put("/v1/databases", plan.database_payload)
        self.put("/v1/databaseSchemas", plan.schema_payload)

    def upsert_table(self, plan: OpenMetadataPushPlan) -> dict:
        self.upsert_hierarchy(plan)
        return self.put("/v1/tables", plan.table_payload)

    def upsert_glossary_term(self, term: dict) -> bool:
        """PUT one glossary term. Returns True on 2xx or already-exists."""
        glossary = str(term.get("glossary") or "Redibis")
        self.ensure_glossary(glossary)
        try:
            self.put("/v1/glossaryTerms", term)
            return True
        except RuntimeError as exc:
            if _is_already_exists_error(exc):
                return True
            raise

    def upsert_glossary_terms(self, plan: OpenMetadataPushPlan) -> int:
        if not plan.glossary_terms:
            return 0
        created = 0
        for term in plan.glossary_terms:
            if self.upsert_glossary_term(term):
                created += 1
        return created

    def upsert_odcs_contract(self, plan: OpenMetadataPushPlan, table_id: str) -> dict:
        """Attach the ODCS document when supported; else create a native OM data contract.

        OpenMetadata builds that expose ``PUT /v1/dataContracts/odcs`` receive the
        full ODCS payload. Older 1.8.x builds only have ``/v1/dataContracts`` —
        fall back to a native CreateDataContract so column definitions / tags
        already on the table still get a linked contract entity.
        """
        try:
            return self._request(
                "PUT", "/v1/dataContracts/odcs", json_body=plan.odcs_document,
                query={"entityId": table_id, "entityType": "table", "mode": "replace"},
            )[1]
        except RuntimeError as exc:
            msg = str(exc)
            if not any(code in msg for code in ("404", "405", "500", "501")):
                raise

        _OM_FIELD_TYPES = {
            "VARCHAR": "STRING",
            "STRING": "STRING",
            "CHAR": "STRING",
            "TEXT": "STRING",
            "INT": "INT",
            "INTEGER": "INT",
            "BIGINT": "LONG",
            "LONG": "LONG",
            "FLOAT": "FLOAT",
            "DOUBLE": "DOUBLE",
            "DECIMAL": "DOUBLE",
            "NUMBER": "DOUBLE",
            "BOOLEAN": "BOOLEAN",
            "BOOL": "BOOLEAN",
            "DATE": "DATE",
            "TIMESTAMP": "TIMESTAMP",
            "DATETIME": "TIMESTAMP",
        }

        safe_name = plan.table.replace(".", "_")
        fields = []
        for col in plan.table_payload.get("columns") or []:
            if not isinstance(col, dict) or not col.get("name"):
                continue
            raw_type = str(col.get("dataType") or "STRING").upper()
            field_body: dict[str, Any] = {
                "name": col["name"],
                "dataType": _OM_FIELD_TYPES.get(raw_type, "STRING"),
                "description": col.get("description") or "",
            }
            if col.get("displayName"):
                field_body["displayName"] = col["displayName"]
            if col.get("dataType"):
                field_body["dataTypeDisplay"] = str(col.get("dataType"))
            fields.append(field_body)

        body: dict[str, Any] = {
            "name": f"{safe_name}_odcs",
            "displayName": f"{plan.table} ODCS",
            "description": (
                f"Native OpenMetadata data contract for {plan.table} "
                "(ODCS /odcs endpoint unavailable on this OM build; "
                "full ODCS remains in the redibis active contract store)."
            )[:1000],
            "status": "Active",
            "entity": {"id": table_id, "type": "table"},
            "schema": fields,
            "extension": {
                "redibis_physicalName": plan.table,
                "redibis_odcs_version": str(
                    (plan.odcs_document or {}).get("version") or "",
                ),
            },
        }

        # Prefer POST create; on conflict return the existing contract for this table.
        try:
            return self._request("POST", "/v1/dataContracts", json_body=body)[1]
        except RuntimeError as post_exc:
            post_msg = str(post_exc)
            if not any(code in post_msg for code in ("400", "409")):
                raise
            existing = self._find_data_contract_for_entity(table_id)
            if existing:
                return existing
            raise

    def _find_data_contract_for_entity(self, table_id: str) -> dict:
        try:
            import httpx
        except ImportError:
            return {}
        url = self._url("/v1/dataContracts")
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                url,
                headers=self._headers(),
                params={"limit": 100},
            )
        if resp.status_code >= 400 or not resp.content:
            return {}
        payload = resp.json()
        for item in payload.get("data") or []:
            ent = item.get("entity") or {}
            if str(ent.get("id") or "") == str(table_id):
                return item
        return {}

    def get_server_version(self) -> tuple[int, int, int]:
        payload = self._request("GET", "/v1/system/version")
        return parse_om_version(payload)

    def find_data_contract_for_entity(self, table_id: str) -> Optional[dict]:
        """Best-effort lookup of an existing data contract for a table entity."""
        try:
            import httpx
        except ImportError as exc:
            raise ImportError("httpx is required — pip install 'redibis[catalog]'") from exc
        # Prefer entity-scoped lookup when available.
        url = self._url("/v1/dataContracts/entity", {"entityId": table_id, "entityType": "table"})
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, headers=self._headers())
        if resp.status_code == 200 and resp.content:
            data = resp.json()
            if isinstance(data, dict) and data.get("id"):
                return data
        # Fallback: paginate contracts (bounded) looking for a matching entity id.
        after: Optional[str] = None
        for _ in range(20):  # cap at ~2000 contracts to bound worst-case cost
            query: dict[str, Any] = {"limit": 100}
            if after:
                query["after"] = after
            listed = self._request("GET", "/v1/dataContracts", query=query)
            items = listed.get("data") if isinstance(listed, dict) else None
            if not isinstance(items, list):
                return None
            for item in items:
                if not isinstance(item, dict):
                    continue
                entity = item.get("entity") or {}
                if isinstance(entity, dict) and str(entity.get("id") or "") == str(table_id):
                    return item
            paging = listed.get("paging") if isinstance(listed, dict) else None
            after = paging.get("after") if isinstance(paging, dict) else None
            if not after or not items:
                break
        return None

    def upsert_native_data_contract(
        self,
        plan: OpenMetadataPushPlan,
        table_id: str,
    ) -> dict:
        payload = build_native_data_contract_payload(
            plan.odcs_document,
            plan.table,
            table_id=table_id,
            table_fqn=plan.table_fqn,
        )
        existing = self.find_data_contract_for_entity(table_id)
        if existing and existing.get("id"):
            # Fail closed on PUT when we know the existing contract id — do not
            # POST a duplicate after a partial update failure.
            payload["id"] = existing["id"]
            return self._request("PUT", "/v1/dataContracts", json_body=payload)
        return self._request("POST", "/v1/dataContracts", json_body=payload)


class OpenMetadataPublisher(CatalogPublisher):
    backend = "openmetadata"

    def __init__(
        self,
        settings: OpenMetadataSettings,
        *,
        ledger_store: Any = None,
        suppression_store: Any = None,
        entity_cache: Any = None,
        audit_store: Any = None,
    ):
        self.settings = settings
        self.ledger_store = ledger_store
        self.suppression_store = suppression_store
        self.entity_cache = entity_cache
        self.audit_store = audit_store
        self._cached_version: Optional[tuple[int, int, int]] = None
        self._cached_strategy: Optional[str] = None

    def attach_stores(
        self,
        *,
        ledger_store: Any = None,
        suppression_store: Any = None,
        entity_cache: Any = None,
        audit_store: Any = None,
    ) -> None:
        if ledger_store is not None:
            self.ledger_store = ledger_store
        if suppression_store is not None:
            self.suppression_store = suppression_store
        if entity_cache is not None:
            self.entity_cache = entity_cache
        if audit_store is not None:
            self.audit_store = audit_store

    @classmethod
    def from_config(cls, config: CatalogConfig) -> "OpenMetadataPublisher":
        om = config.openmetadata
        host = os.environ.get("OM_HOST", om.host)
        jwt_env = om.jwt_env or "OM_BOT_JWT"
        jwt = os.environ.get(jwt_env, "")
        # Validate early so dry-run/live share the same failure mode when JWT is set.
        # Empty is allowed until a live push constructs the HTTP client.
        if jwt.strip():
            jwt = _validate_om_jwt(jwt, env_name=jwt_env)
        return cls(OpenMetadataSettings(
            host=host,
            jwt=jwt,
            service_name=om.service_name,
            default_schema=om.default_schema,
            entity_mode=getattr(om, "entity_mode", None)
            or getattr(om, "mode", None)
            or "mixed",
            fqn_map=dict(getattr(om, "fqn_map", None) or {}),
            search_services=list(getattr(om, "search_services", None) or []),
            min_version=getattr(om, "min_version", "1.12.4") or "1.12.4",
            publish_threshold=float(getattr(om, "publish_threshold", 0.60)),
            confirm_threshold=float(getattr(om, "confirm_threshold", 0.85)),
            delete_after_negatives=int(getattr(om, "delete_after_negatives", 2)),
            resolve_cache_ttl_hours=int(
                getattr(om, "resolve_cache_ttl_hours", 168) or 168
            ),
        ))

    def _plan(self, contract: dict, table: str, options: CatalogPushOptions) -> OpenMetadataPushPlan:
        return build_openmetadata_plan(
            contract, table,
            service_name=self.settings.service_name,
            default_schema=self.settings.default_schema,
            options=options,
            confirm_threshold=self.settings.confirm_threshold,
        )

    def _desired_and_coverage(
        self,
        contract: dict,
        table: str,
        *,
        asset_fqn: str,
        options: CatalogPushOptions,
    ) -> tuple[list[Assertion], list[CoverageRecord]]:
        scan_coverage = options.scan_coverage
        if scan_coverage is not None:
            from redibis.services.catalog.assertions import rebind_coverage_asset_fqn

            scan_coverage = rebind_coverage_asset_fqn(list(scan_coverage), asset_fqn)
        return build_assertions_from_contract(
            contract,
            table,
            asset_fqn=asset_fqn,
            scan_id="",
            classification_results=options.classification_results,
            include_tags=options.tags,
            include_glossary=options.glossary,
            publish_threshold=self.settings.publish_threshold,
            column_telemetry=options.column_telemetry,
            metadata_store=options.metadata_store,
            scan_coverage=scan_coverage,
        )

    def _local_reconcile(
        self,
        contract: dict,
        table: str,
        *,
        asset_fqn: str,
        options: CatalogPushOptions,
        remote: Optional[list[Assertion]] = None,
    ):
        desired, coverage = self._desired_and_coverage(
            contract, table, asset_fqn=asset_fqn, options=options,
        )
        ledger: list[LedgerEntry] = []
        suppressions = []
        if self.ledger_store is not None:
            try:
                ledger = self.ledger_store.get_entries(table, self.backend)
            except Exception:  # noqa: BLE001
                ledger = []
        if self.suppression_store is not None:
            try:
                suppressions = self.suppression_store.list(table, self.backend)
            except Exception:  # noqa: BLE001
                suppressions = []
        mode = (
            ReconcileMode.ENFORCE
            if (options.reconcile_mode or "").lower() == "enforce"
            else ReconcileMode.NORMAL
        )
        policy = _reconcile_policy_from_settings(self.settings)
        if options.clear_suppressions:
            from dataclasses import replace as _replace
            policy = _replace(policy, clear_suppressions=True)
        return reconcile(
            backend=self.backend,
            asset_fqn=asset_fqn,
            desired=desired,
            remote=list(remote or []),
            ledger=ledger,
            coverage=coverage,
            suppressions=suppressions,
            mode=mode,
            policy=policy,
        )

    def preview(self, contract: dict, table: str, *, options: CatalogPushOptions) -> dict[str, Any]:
        plan = self._plan(contract, table, options)
        preview = plan_to_preview(plan, options)
        rplan = self._local_reconcile(
            contract, table, asset_fqn=plan.table_fqn, options=options, remote=[],
        )
        preview["reconcile"] = {
            "summary": rplan.summary,
            "diff": render_reconcile_plan(rplan, table=table),
            "ops": len(rplan.ops),
            "conflicts": len(rplan.conflicts),
        }
        return preview

    def _push_managed(
        self,
        client: _OpenMetadataClient,
        plan: OpenMetadataPushPlan,
        options: CatalogPushOptions,
    ) -> CatalogPushResult:
        client.ensure_policy_tags(plan)
        table_entity = client.upsert_table(plan)
        table_id = table_entity.get("id", "")

        glossary_count = 0
        if options.glossary:
            glossary_count = client.upsert_glossary_terms(plan)

        contract_id = ""
        contract_mode = ""
        if options.contract and table_id:
            strategy, version_tuple = self._resolve_contract_strategy(client)
            if strategy == "odcs":
                contract_entity = client.upsert_odcs_contract(plan, table_id)
            else:
                contract_entity = client.upsert_native_data_contract(plan, table_id)
            contract_id = contract_entity.get("id", "")
            contract_mode = strategy

        preview = plan_to_preview(plan, options)
        return CatalogPushResult(
            table=plan.table,
            backend=self.backend,
            entity_fqn=plan.table_fqn,
            entity_id=table_id,
            contract_id=contract_id,
            glossary_count=glossary_count,
            preview=preview,
            details={
                "table_id": table_id,
                "contract_id": contract_id,
                "contract_mode": contract_mode,
                "policy_tags": (preview or {}).get("policy_tags") or [],
                "is_redibis_managed": True,
                "write_mode": "put",
            },
        )

    def _push_enrich(
        self,
        client: _OpenMetadataClient,
        contract: dict,
        table: str,
        *,
        target_fqn: str,
        plan: OpenMetadataPushPlan,
        options: CatalogPushOptions,
    ) -> CatalogPushResult:
        max_attempts = 3
        last_plan = None
        table_entity: dict = {}
        succeeded_gloss_ops: list[IntentOp] = []
        glossary_count = 0
        gloss_put_error: Optional[BaseException] = None

        for attempt in range(max_attempts):
            table_entity = client.get_table(
                target_fqn, fields="columns,tags,owners,version",
            )
            ledger: list[LedgerEntry] = []
            suppressions = []
            if self.ledger_store is not None:
                ledger = self.ledger_store.get_entries(table, self.backend)
            if self.suppression_store is not None:
                suppressions = self.suppression_store.list(table, self.backend)

            remote = remote_assertions_from_table(
                table_entity, ledger_entries=ledger,
            )
            desired, coverage = self._desired_and_coverage(
                contract, table, asset_fqn=target_fqn, options=options,
            )
            mode = (
                ReconcileMode.ENFORCE
                if (options.reconcile_mode or "").lower() == "enforce"
                else ReconcileMode.NORMAL
            )
            policy = _reconcile_policy_from_settings(self.settings)
            if options.clear_suppressions:
                from dataclasses import replace as _replace
                policy = _replace(policy, clear_suppressions=True)
            rplan = reconcile(
                backend=self.backend,
                asset_fqn=target_fqn,
                desired=desired,
                remote=remote,
                ledger=ledger,
                coverage=coverage,
                suppressions=suppressions,
                mode=mode,
                policy=policy,
            )
            last_plan = rplan

            # Glossary term PUTs from reconciler ops (not plan.glossary_terms).
            # Only successful PUTs are patched as TagLabels and ledgered.
            succeeded_gloss_ops = []
            glossary_count = 0
            gloss_put_error = None
            if options.glossary:
                gloss_write_ops = [
                    op
                    for op in rplan.ops
                    if op.key.facet is Facet.GLOSSARY_LINK
                    and op.op in ("add", "update", "promote")
                ]
                for op in gloss_write_ops:
                    term = _glossary_term_payload_from_op(op, table_fqn=target_fqn)
                    try:
                        if client.upsert_glossary_term(term):
                            succeeded_gloss_ops.append(op)
                            glossary_count += 1
                    except RuntimeError as exc:
                        gloss_put_error = exc
                        break

            succeeded_gloss_keys = {op.key for op in succeeded_gloss_ops}
            patch_ops = [
                op
                for op in rplan.ops
                if op.key.facet is not Facet.GLOSSARY_LINK
                or op.key in succeeded_gloss_keys
                or op.op == "remove"
            ]
            client.ensure_tag_steps(_tag_setup_from_ops(patch_ops))
            patch = build_json_patch(patch_ops, table_entity)

            if not patch:
                break
            try:
                table_entity = client.patch_table(target_fqn, patch)
                break
            except OpenMetadataConflictError:
                if attempt >= max_attempts - 1:
                    raise
                continue

        # Audited enforce overrides
        if (
            last_plan is not None
            and (options.reconcile_mode or "").lower() == "enforce"
            and self.audit_store is not None
        ):
            for op in last_plan.ops:
                if op.displaces is None and op.op not in ("update", "add", "remove"):
                    continue
                if op.displaces is None and op.op != "update":
                    continue
                self.audit_store.append(
                    table,
                    self.backend,
                    {
                        "actor": options.enforce_actor or "cli",
                        "reason": options.enforce_reason,
                        "op": op.op,
                        "key": {
                            "asset_fqn": op.key.asset_fqn,
                            "column_path": op.key.column_path,
                            "facet": op.key.facet.value,
                            "value_key": op.key.value_key,
                        },
                        "prior": op.prior,
                        "prior_authority": (
                            op.displaces.value if op.displaces is not None else None
                        ),
                        "value": op.value,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                )
            if options.clear_suppressions and self.suppression_store is not None:
                for op in last_plan.ops:
                    self.suppression_store.remove(table, self.backend, op.key)

        # Ledger: non-glossary patch ops + glossary ops whose term PUT was 2xx.
        ledger_ops: list[IntentOp] = []
        if last_plan is not None:
            ledger_ops.extend(
                op for op in last_plan.ops if op.key.facet is not Facet.GLOSSARY_LINK
            )
            ledger_ops.extend(succeeded_gloss_ops)
            if options.glossary:
                ledger_ops.extend(
                    op
                    for op in last_plan.ops
                    if op.key.facet is Facet.GLOSSARY_LINK and op.op == "remove"
                )

        if last_plan is not None and self.ledger_store is not None:
            upserts, removals = _ledger_updates_from_ops(
                ledger_ops, backend=self.backend,
            )
            if removals:
                self.ledger_store.remove_keys(table, self.backend, removals)
            if upserts:
                self.ledger_store.upsert_entries(table, self.backend, upserts)

        if gloss_put_error is not None:
            raise gloss_put_error

        contract_id = ""
        contract_mode = ""
        table_id = str(table_entity.get("id") or "")
        if options.contract and table_id:
            # Align plan FQN for contract attach
            plan.table_fqn = target_fqn
            strategy, version_tuple = self._resolve_contract_strategy(client)
            if strategy == "odcs":
                contract_entity = client.upsert_odcs_contract(plan, table_id)
            else:
                contract_entity = client.upsert_native_data_contract(plan, table_id)
            contract_id = contract_entity.get("id", "")
            contract_mode = strategy

        preview = plan_to_preview(plan, options)
        if last_plan is not None:
            preview["reconcile"] = {
                "summary": last_plan.summary,
                "diff": render_reconcile_plan(last_plan, table=table),
                "ops": len(last_plan.ops),
                "conflicts": len(last_plan.conflicts),
            }
            preview["table_fqn"] = target_fqn

        return CatalogPushResult(
            table=table,
            backend=self.backend,
            entity_fqn=target_fqn,
            entity_id=table_id,
            contract_id=contract_id,
            glossary_count=glossary_count,
            preview=preview,
            details={
                "table_id": table_id,
                "contract_id": contract_id,
                "contract_mode": contract_mode,
                "policy_tags": (preview or {}).get("policy_tags") or [],
                "is_redibis_managed": False,
                "write_mode": "patch",
                "reconcile_summary": (last_plan.summary if last_plan else {}),
            },
        )

    def _resolve_contract_strategy(self, client: _OpenMetadataClient) -> tuple[str, tuple[int, int, int]]:
        if self._cached_strategy and self._cached_version is not None:
            return self._cached_strategy, self._cached_version
        version = client.get_server_version()
        strategy = "odcs" if uses_odcs_data_contract_api(version) else "native"
        self._cached_version = version
        self._cached_strategy = strategy
        return strategy, version

    def push(
        self,
        contract: dict,
        table: str,
        *,
        options: CatalogPushOptions,
        dry_run: bool = False,
    ) -> CatalogPushResult:
        plan = self._plan(contract, table, options)
        preview = plan_to_preview(plan, options)

        # Always attach a local reconcile preview (remote=[] when offline).
        rplan = self._local_reconcile(
            contract, table, asset_fqn=plan.table_fqn, options=options, remote=[],
        )
        preview["reconcile"] = {
            "summary": rplan.summary,
            "diff": render_reconcile_plan(rplan, table=table),
            "ops": len(rplan.ops),
            "conflicts": len(rplan.conflicts),
        }

        if dry_run:
            return CatalogPushResult(
                table=table,
                backend=self.backend,
                entity_fqn=plan.table_fqn,
                dry_run=True,
                preview=preview,
            )

        client = _OpenMetadataClient.from_settings(self.settings)
        version_info = client.check_min_version()

        # Resolve target FQN (enrich vs create)
        target_fqn, is_managed = resolve_table_fqn(
            client,
            table,
            self.settings,
            cache=self.entity_cache,
            backend=self.backend,
            refresh=bool(options.refresh_entity),
        )
        plan.table_fqn = target_fqn

        if is_managed:
            # Ensure plan hierarchy matches constructed managed FQN
            result = self._push_managed(client, plan, options)
        else:
            result = self._push_enrich(
                client, contract, table,
                target_fqn=target_fqn, plan=plan, options=options,
            )

        result.details = {
            **(result.details or {}),
            "version": version_info,
            "resolved_fqn": target_fqn,
            "is_redibis_managed": is_managed,
        }

        if options.quality and not dry_run:
            from redibis.services.catalog.openmetadata_quality import (
                iter_contract_quality_rules,
                map_results_for_publish,
                publish_quality,
            )

            mapped: list[dict] = []
            if options.quality_results:
                mapped = map_results_for_publish({"results": options.quality_results})
            if mapped or iter_contract_quality_rules(contract, table):
                tel = publish_quality(
                    client,
                    contract,
                    table,
                    table_fqn=target_fqn,
                    results=mapped or None,
                    ledger_store=self.ledger_store,
                    artifact_ref=options.quality_artifact_ref,
                    run_id=options.quality_run_id,
                )
                result.details["quality_telemetry"] = tel

        return result

    def managed_table_fqn(self, table: str) -> str:
        """``schema.table`` → ``{service}.{database}.{default_schema}.{table}``."""
        database, table_name = split_physical_name(table)
        return (
            f"{self.settings.service_name}.{database}."
            f"{self.settings.default_schema}.{table_name}"
        )

    def delete_tables(
        self,
        tables: Optional[list[str]] = None,
        *,
        fqns: Optional[list[str]] = None,
        dry_run: bool = True,
        hard_delete: bool = True,
    ) -> dict[str, Any]:
        """Delete one or more OM tables by ``schema.table`` and/or raw FQN."""
        targets: list[str] = []
        seen: set[str] = set()
        for table in tables or []:
            fqn = self.managed_table_fqn(table)
            if fqn not in seen:
                seen.add(fqn)
                targets.append(fqn)
        for fqn in fqns or []:
            fqn = (fqn or "").strip()
            if fqn and fqn not in seen:
                seen.add(fqn)
                targets.append(fqn)
        if not targets:
            raise ValueError("Provide at least one TABLE or --fqn")

        plan = {
            "backend": self.backend,
            "dry_run": dry_run,
            "hard_delete": hard_delete,
            "tables": targets,
            "actions": [
                {
                    "kind": "table",
                    "entity_fqn": fqn,
                    "method": "DELETE",
                    "path": f"/v1/tables/name/{quote(fqn, safe='')}",
                }
                for fqn in targets
            ],
        }
        if dry_run:
            return plan

        client = _OpenMetadataClient.from_settings(self.settings)
        results = []
        for fqn in targets:
            results.append(client.delete_table_fqn(
                fqn, hard_delete=hard_delete, recursive=True,
            ))
        plan["results"] = results
        plan["deleted"] = sum(1 for r in results if not r.get("missing"))
        plan["missing"] = sum(1 for r in results if r.get("missing"))
        return plan

    def wipe(
        self,
        *,
        dry_run: bool = True,
        hard_delete: bool = True,
        with_glossary: bool = False,
        with_classifications: bool = False,
        service_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Wipe the configured OpenMetadata database service (and optional tags).

        Deletes ``databaseServices/{service}`` with ``recursive=true``, which
        removes databases / schemas / tables under that service in one call.
        """
        service = (service_name or self.settings.service_name or "redibis").strip()
        if not service:
            raise ValueError("service_name is required for wipe")

        client = None if dry_run else _OpenMetadataClient.from_settings(self.settings)
        listed: list[str] = []
        if client is not None:
            listed = [
                str(row.get("fullyQualifiedName") or row.get("name") or "")
                for row in client.list_tables_for_service(service)
                if isinstance(row, dict)
            ]
            listed = [x for x in listed if x]

        actions: list[dict[str, Any]] = [{
            "kind": "databaseService",
            "entity_fqn": service,
            "method": "DELETE",
            "path": f"/v1/services/databaseServices/name/{quote(service, safe='')}",
            "recursive": True,
            "note": "removes databases/schemas/tables under this service",
        }]
        if with_glossary:
            actions.append({
                "kind": "glossary",
                "entity_fqn": "Redibis",
                "method": "DELETE",
                "path": "/v1/glossaries/name/Redibis",
                "recursive": True,
            })
        if with_classifications:
            for name in ("Redibis", "RedibisPolicy"):
                actions.append({
                    "kind": "classification",
                    "entity_fqn": name,
                    "method": "DELETE",
                    "path": f"/v1/classifications/name/{quote(name, safe='')}",
                })

        plan = {
            "backend": self.backend,
            "dry_run": dry_run,
            "hard_delete": hard_delete,
            "service_name": service,
            "listed_tables": listed,
            "with_glossary": with_glossary,
            "with_classifications": with_classifications,
            "actions": actions,
        }
        if dry_run:
            return plan

        assert client is not None
        results = [client.delete_database_service(
            service, hard_delete=hard_delete, recursive=True,
        )]
        if with_glossary:
            results.append(client.delete_glossary(
                "Redibis", hard_delete=hard_delete, recursive=True,
            ))
        if with_classifications:
            for name in ("Redibis", "RedibisPolicy"):
                results.append(client.delete_classification(
                    name, hard_delete=hard_delete,
                ))
        plan["results"] = results
        plan["deleted"] = sum(1 for r in results if not r.get("missing"))
        plan["missing"] = sum(1 for r in results if r.get("missing"))
        return plan
