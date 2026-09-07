"""Apache Atlas catalog publisher (concrete backend)."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from redibis.config import AtlasCatalogConfig, CatalogConfig, ConfigError
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
class AtlasPushPlan:
    table: str
    entity_qualified_name: str
    database_qualified_name: str
    database_entity: dict
    table_entity: dict
    classifications: list[str]
    classification_payloads: list[dict] = field(default_factory=list)
    glossary_terms: list[dict] = field(default_factory=list)
    odcs_document: dict = field(default_factory=dict)


@dataclass
class AtlasSettings:
    host: str = "http://localhost:21000"
    username: str = "admin"
    password: str = ""
    cluster: str = "redibis"
    entity_type: str = "hive_table"


def _atlas_api_base(host: str) -> str:
    base = host.rstrip("/")
    if "/api/atlas" in base:
        return base
    return f"{base}/api/atlas/v2"


def atlas_qualified_name(database: str, table_name: str, cluster: str) -> str:
    return f"{database}.{table_name}@{cluster}"


def atlas_database_qualified_name(database: str, cluster: str) -> str:
    return f"{database}@{cluster}"


def build_atlas_plan(
    contract: dict,
    table: str,
    *,
    cluster: str = "redibis",
    entity_type: str = "hive_table",
    options: Optional[CatalogPushOptions] = None,
    classification_results: Optional[list] = None,
) -> AtlasPushPlan:
    options = options or CatalogPushOptions()
    if classification_results is None:
        classification_results = options.classification_results
    meta = table_semantics(contract, table)
    database, table_name = split_physical_name(table)
    db_qn = atlas_database_qualified_name(database, cluster)
    table_qn = atlas_qualified_name(database, table_name, cluster)

    cols = []
    table_classifications: set[str] = set()
    for col in iter_columns(
        contract, table, classification_results=classification_results,
    ):
        comment = col.description
        tag_names = column_catalog_tag_names(col, include_tags=options.tags)
        if tag_names:
            comment = f"[{' | '.join(tag_names)}] {comment}".strip()
            table_classifications.update(tag_names)
        cols.append({
            "typeName": "hive_column",
            "attributes": {
                "name": col.name,
                "type": col.logical_type or "string",
                "comment": comment[:512],
            },
        })

    odcs_doc = copy.deepcopy(contract)
    odcs_doc.setdefault("physicalName", table)
    parameters: dict[str, str] = {}
    if options.contract:
        parameters["redibis.odcs.contract"] = json.dumps(odcs_doc)[:65000]

    table_entity = {
        "entity": {
            "typeName": entity_type,
            "attributes": {
                "qualifiedName": table_qn,
                "name": table_name,
                "description": meta.description[:4000],
                "db": {
                    "typeName": "hive_db",
                    "uniqueAttributes": {"qualifiedName": db_qn},
                },
                "sd": {
                    "typeName": "hive_storagedesc",
                    "attributes": {"cols": cols},
                },
                **({"parameters": parameters} if parameters else {}),
            },
            **({"classifications": [{"typeName": c} for c in sorted(table_classifications)]}
               if table_classifications and options.tags else {}),
        },
    }

    database_entity = {
        "entity": {
            "typeName": "hive_db",
            "attributes": {
                "qualifiedName": db_qn,
                "name": database,
                "description": f"Database {database} (redibis ODCS)",
            },
        },
    }

    glossary: list[dict] = []
    if options.glossary:
        for col in iter_columns(
            contract, table, classification_results=classification_results,
        ):
            display = col.business_name or f"{table}.{col.name}"
            glossary.append({
                "name": f"{table.replace('.', '_')}_{col.name}",
                "shortDescription": display,
                "longDescription": col.description or display,
                "glossary": "Redibis",
            })

    classification_payloads: list[dict] = []
    if classification_results:
        from redibis.classification.atlas_bridge import classifications_for_atlas

        seen: set[str] = set()
        for result in classification_results:
            for payload in classifications_for_atlas(result):
                key = payload.get("typeName", "")
                if key and key not in seen:
                    seen.add(key)
                    classification_payloads.append(payload)
                    table_classifications.add(key)

    return AtlasPushPlan(
        table=table,
        entity_qualified_name=table_qn,
        database_qualified_name=db_qn,
        database_entity=database_entity,
        table_entity=table_entity,
        classifications=sorted(table_classifications),
        classification_payloads=classification_payloads,
        glossary_terms=glossary,
        odcs_document=odcs_doc,
    )


def plan_to_preview(plan: AtlasPushPlan, options: CatalogPushOptions) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [
        {"target": "hive_db", "method": "POST", "path": "/entity", "body": plan.database_entity},
        {"target": "hive_table", "method": "POST", "path": "/entity", "body": plan.table_entity},
    ]
    if options.glossary and plan.glossary_terms:
        steps.append({
            "target": "glossaryTerms",
            "method": "POST",
            "path": "/glossary/terms",
            "body": plan.glossary_terms,
        })
    if plan.classification_payloads:
        steps.append({
            "target": "classifications",
            "method": "POST",
            "path": "/entity/guid/{guid}/classifications",
            "body": plan.classification_payloads,
            "atomic": True,
            "verify_readback": True,
        })
    return {
        "backend": "atlas",
        "entity_qualified_name": plan.entity_qualified_name,
        "classifications": plan.classifications,
        "steps": steps,
    }


class _AtlasClient:
    def __init__(self, settings: AtlasSettings, *, timeout: float = 60.0):
        self.base = _atlas_api_base(settings.host)
        self.auth = (settings.username, settings.password)
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: AtlasSettings) -> "_AtlasClient":
        if not settings.password:
            raise ConfigError(
                "Atlas password missing — set catalog.atlas.password_env or ATLAS_PASSWORD"
            )
        return cls(settings)

    def post(self, path: str, body: Any) -> dict:
        return catalog_http_request(
            "POST",
            join_url(self.base, path),
            json_body=body,
            auth=self.auth,
            timeout=self.timeout,
        )

    def get(self, path: str) -> dict:
        try:
            return catalog_http_request(
                "GET", join_url(self.base, path), auth=self.auth, timeout=self.timeout,
            )
        except RuntimeError as exc:
            if "404" in str(exc):
                return {}
            raise

    def ensure_glossary(self, name: str = "Redibis") -> dict:
        existing = self.get(f"/glossary/name/{quote(name)}")
        if existing.get("guid"):
            return existing
        return self.post("/glossary", {
            "name": name,
            "shortDescription": name,
            "longDescription": "Business terms pushed from redibis ODCS contracts",
        })

    def upsert_entities(self, plan: AtlasPushPlan) -> dict:
        self.post("/entity", plan.database_entity)
        return self.post("/entity", plan.table_entity)

    def apply_classifications_atomic(
        self,
        entity_guid: str,
        classifications: list[dict],
    ) -> dict:
        """Apply all co-tags in one POST (golden rule 3 — atomicity)."""
        if not classifications:
            return {}
        return self.post(
            f"/entity/guid/{quote(entity_guid)}/classifications",
            {"classifications": classifications},
        )

    def verify_classifications(
        self,
        entity_guid: str,
        expected_type_names: list[str],
    ) -> dict:
        """Read-back verification after classification push."""
        entity = self.get(f"/entity/guid/{quote(entity_guid)}")
        applied = {
            c.get("typeName")
            for c in (entity.get("entity", {}).get("classifications") or [])
            if c.get("typeName")
        }
        missing = sorted(set(expected_type_names) - applied)
        return {
            "verified": not missing,
            "applied": sorted(applied),
            "missing": missing,
        }

    def upsert_glossary_terms(self, plan: AtlasPushPlan) -> int:
        if not plan.glossary_terms:
            return 0
        glossary = self.ensure_glossary()
        guid = glossary.get("guid", "")
        count = 0
        for term in plan.glossary_terms:
            payload = dict(term)
            payload["anchor"] = {"glossaryGuid": guid}
            self.post("/glossary/terms", payload)
            count += 1
        return count


class AtlasPublisher(CatalogPublisher):
    backend = "atlas"

    def __init__(self, settings: AtlasSettings):
        self.settings = settings

    @classmethod
    def from_config(cls, config: CatalogConfig) -> "AtlasPublisher":
        ac = config.atlas
        host = os.environ.get("ATLAS_HOST", ac.host)
        user = os.environ.get(ac.username_env or "ATLAS_USERNAME", "admin")
        pwd_env = ac.password_env or "ATLAS_PASSWORD"
        password = os.environ.get(pwd_env, "")
        return cls(AtlasSettings(
            host=host,
            username=user,
            password=password,
            cluster=ac.cluster,
            entity_type=ac.entity_type,
        ))

    def _plan(self, contract: dict, table: str, options: CatalogPushOptions) -> AtlasPushPlan:
        return build_atlas_plan(
            contract,
            table,
            cluster=self.settings.cluster,
            entity_type=self.settings.entity_type,
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
                entity_fqn=plan.entity_qualified_name,
                dry_run=True,
                preview=preview,
            )

        client = _AtlasClient.from_settings(self.settings)
        entity_resp = client.upsert_entities(plan)
        guid = ""
        mutated = entity_resp.get("mutatedEntities") or {}
        for bucket in ("CREATE", "UPDATE"):
            for ent in mutated.get(bucket) or []:
                if ent.get("typeName") == self.settings.entity_type:
                    guid = ent.get("guid", guid)

        glossary_count = 0
        if options.glossary:
            glossary_count = client.upsert_glossary_terms(plan)

        classification_verify = {}
        if guid and plan.classification_payloads:
            client.apply_classifications_atomic(guid, plan.classification_payloads)
            classification_verify = client.verify_classifications(
                guid,
                [p["typeName"] for p in plan.classification_payloads],
            )

        return CatalogPushResult(
            table=table,
            backend=self.backend,
            entity_fqn=plan.entity_qualified_name,
            entity_id=guid,
            glossary_count=glossary_count,
            preview=preview,
            details={
                "guid": guid,
                "classifications": plan.classifications,
                "classification_verify": classification_verify,
            },
        )
