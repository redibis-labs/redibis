"""DataHub catalog publisher (concrete backend)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.config import CatalogConfig, ConfigError, DataHubCatalogConfig
from redibis.services.catalog._http import catalog_http_request, join_url
from redibis.services.catalog.base import (
    CatalogPublisher,
    CatalogPushOptions,
    CatalogPushResult,
)
from redibis.services.catalog.mapping import (
    column_catalog_tag_names,
    iter_columns,
    split_physical_name,
    table_semantics,
)


@dataclass
class DataHubPushPlan:
    table: str
    dataset_urn: str
    platform_urn: str
    upsert_payload: list[dict]
    glossary_terms: list[dict] = field(default_factory=list)
    odcs_document: dict = field(default_factory=dict)


@dataclass
class DataHubSettings:
    host: str = "http://localhost:8080"
    token: str = ""
    platform: str = "redibis"
    env: str = "PROD"
    actor: str = "urn:li:corpuser:redibis"


def _datahub_openapi_base(host: str) -> str:
    base = host.rstrip("/")
    if base.endswith("/openapi/entities/v1"):
        return base
    return f"{base}/openapi/entities/v1"


def dataset_urn(platform: str, table: str, env: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{table},{env})"


def platform_urn(platform: str) -> str:
    return f"urn:li:dataPlatform:{platform}"


def _datahub_field_type(logical: str) -> dict:
    type_name = {
        "string": "StringType",
        "str": "StringType",
        "text": "StringType",
        "integer": "NumberType",
        "int": "NumberType",
        "long": "NumberType",
        "float": "NumberType",
        "double": "NumberType",
        "decimal": "NumberType",
        "number": "NumberType",
        "boolean": "BooleanType",
        "bool": "BooleanType",
        "date": "DateType",
        "timestamp": "TimeType",
        "datetime": "TimeType",
    }.get((logical or "string").lower(), "StringType")
    return {"type": {"__type": type_name}}


def _schema_hash(fields: list[dict]) -> str:
    raw = json.dumps(fields, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def build_datahub_plan(
    contract: dict,
    table: str,
    *,
    platform: str = "redibis",
    env: str = "PROD",
    actor: str = "urn:li:corpuser:redibis",
    options: Optional[CatalogPushOptions] = None,
) -> DataHubPushPlan:
    options = options or CatalogPushOptions()
    meta = table_semantics(contract, table)
    urn = dataset_urn(platform, table, env)
    plat = platform_urn(platform)
    now = int(time.time() * 1000)

    schema_fields: list[dict] = []
    dataset_tags: set[str] = set()
    for col in iter_columns(
        contract, table, classification_results=options.classification_results,
    ):
        field_payload: dict[str, Any] = {
            "fieldPath": col.name,
            "nullable": True,
            "description": col.description,
            "type": _datahub_field_type(col.logical_type),
            "nativeDataType": col.physical_type or col.logical_type or "string",
            "recursive": False,
        }
        tag_names = column_catalog_tag_names(col, include_tags=options.tags)
        if tag_names:
            field_payload["globalTags"] = {
                "tags": [{"tag": f"urn:li:tag:{name}"} for name in tag_names],
            }
            dataset_tags.update(tag_names)
        if options.glossary and col.business_name:
            term_urn = f"urn:li:glossaryTerm:Redibis.{table.replace('.', '_')}.{col.name}"
            field_payload["glossaryTerms"] = {"terms": [{"urn": term_urn}]}
        schema_fields.append(field_payload)

    odcs_doc = copy.deepcopy(contract)
    odcs_doc.setdefault("physicalName", table)

    custom_properties: dict[str, str] = {}
    if options.contract:
        custom_properties["odcs_contract"] = json.dumps(odcs_doc)[:65000]

    upsert: list[dict] = [
        {
            "entityType": "dataset",
            "entityUrn": urn,
            "aspect": {
                "__type": "datasetProperties",
                "description": meta.description[:4000],
                **({"customProperties": custom_properties} if custom_properties else {}),
            },
        },
        {
            "entityType": "dataset",
            "entityUrn": urn,
            "aspect": {
                "__type": "schemaMetadata",
                "schemaName": meta.schema_name,
                "platform": plat,
                "version": 0,
                "hash": _schema_hash(schema_fields),
                "platformSchema": {"__type": "OtherSchema", "rawSchema": meta.schema_name},
                "fields": schema_fields,
                "created": {"time": now, "actor": actor},
                "lastModified": {"time": now, "actor": actor},
            },
        },
    ]

    if options.tags and dataset_tags:
        upsert.append({
            "entityType": "dataset",
            "entityUrn": urn,
            "aspect": {
                "__type": "globalTags",
                "tags": [{"tag": f"urn:li:tag:{name}"} for name in sorted(dataset_tags)],
            },
        })

    glossary: list[dict] = []
    if options.glossary:
        for col in iter_columns(
            contract, table, classification_results=options.classification_results,
        ):
            display = col.business_name or f"{table}.{col.name}"
            term_urn = f"urn:li:glossaryTerm:Redibis.{table.replace('.', '_')}.{col.name}"
            glossary.append({
                "entityType": "glossaryTerm",
                "entityUrn": term_urn,
                "aspect": {
                    "__type": "glossaryTermInfo",
                    "definition": col.description or display,
                    "name": display,
                },
            })

    return DataHubPushPlan(
        table=table,
        dataset_urn=urn,
        platform_urn=plat,
        upsert_payload=upsert,
        glossary_terms=glossary,
        odcs_document=odcs_doc,
    )


def plan_to_preview(plan: DataHubPushPlan, options: CatalogPushOptions) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [
        {
            "target": "datasetAspects",
            "method": "POST",
            "path": "/openapi/entities/v1/",
            "body": plan.upsert_payload,
        },
    ]
    if options.glossary and plan.glossary_terms:
        steps.append({
            "target": "glossaryTerms",
            "method": "POST",
            "path": "/openapi/entities/v1/",
            "body": plan.glossary_terms,
        })
    return {
        "backend": "datahub",
        "dataset_urn": plan.dataset_urn,
        "steps": steps,
    }


class _DataHubClient:
    def __init__(self, settings: DataHubSettings, *, timeout: float = 60.0):
        self.url = _datahub_openapi_base(settings.host) + "/"
        self.token = settings.token
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: DataHubSettings) -> "_DataHubClient":
        if not settings.token:
            raise ConfigError(
                "DataHub token missing — set catalog.datahub.token_env or DATAHUB_GMS_TOKEN"
            )
        return cls(settings)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def upsert(self, payload: list[dict]) -> dict:
        return catalog_http_request(
            "POST", self.url, json_body=payload, headers=self._headers(), timeout=self.timeout,
        )

    def push_plan(self, plan: DataHubPushPlan, options: CatalogPushOptions) -> dict:
        result = self.upsert(plan.upsert_payload)
        if options.glossary and plan.glossary_terms:
            self.upsert(plan.glossary_terms)
        return result


class DataHubPublisher(CatalogPublisher):
    backend = "datahub"

    def __init__(self, settings: DataHubSettings):
        self.settings = settings

    @classmethod
    def from_config(cls, config: CatalogConfig) -> "DataHubPublisher":
        dh = config.datahub
        host = os.environ.get("DATAHUB_GMS_HOST", dh.host)
        token_env = dh.token_env or "DATAHUB_GMS_TOKEN"
        token = os.environ.get(token_env, "")
        actor = os.environ.get("DATAHUB_ACTOR_URN", dh.actor)
        return cls(DataHubSettings(
            host=host,
            token=token,
            platform=dh.platform,
            env=dh.env,
            actor=actor,
        ))

    def _plan(self, contract: dict, table: str, options: CatalogPushOptions) -> DataHubPushPlan:
        return build_datahub_plan(
            contract,
            table,
            platform=self.settings.platform,
            env=self.settings.env,
            actor=self.settings.actor,
            options=options,
        )

    def preview(self, contract: dict, table: str, *, options: CatalogPushOptions) -> dict[str, Any]:
        return plan_to_preview(self._plan(contract, table, options), options)

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

        if dry_run:
            return CatalogPushResult(
                table=table,
                backend=self.backend,
                entity_fqn=plan.dataset_urn,
                dry_run=True,
                preview=preview,
            )

        client = _DataHubClient.from_settings(self.settings)
        client.push_plan(plan, options)

        return CatalogPushResult(
            table=table,
            backend=self.backend,
            entity_fqn=plan.dataset_urn,
            glossary_count=len(plan.glossary_terms) if options.glossary else 0,
            preview=preview,
            details={"dataset_urn": plan.dataset_urn},
        )
