"""Catalog push mapping tests (neutral API; OpenMetadata, Atlas, DataHub backends)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from redibis.services.catalog.atlas import AtlasPublisher, AtlasSettings, build_atlas_plan
from redibis.services.catalog.base import CatalogPushOptions
from redibis.services.catalog.datahub import (
    DataHubPublisher,
    DataHubSettings,
    build_datahub_plan,
    dataset_urn,
)
from redibis.services.catalog.mapping import (
    load_contract_file,
    resolve_contract_table,
    split_physical_name,
    table_semantics,
)
from redibis.services.catalog.openmetadata import (
    OpenMetadataPublisher,
    OpenMetadataSettings,
    build_openmetadata_plan,
    openmetadata_ui_url,
    plan_to_preview as om_preview,
)
from redibis.services.catalog.registry import available_backends, get_publisher
from redibis.config import CatalogConfig
from redibis.services.catalog_service import CatalogService
from redibis.store.contract_metadata import ContractMetadataStore
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
TABLE = "telecom.customers"
GOLDEN_OM = Path(__file__).parent / "fixtures" / "catalog_om_preview.json"


@pytest.fixture
def contract() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def test_available_backends():
    backends = available_backends()
    assert backends == ["atlas", "datahub", "openmetadata"]


def test_get_publisher_from_config():
    pub = get_publisher(CatalogConfig())
    assert pub.backend == "openmetadata"


def test_split_physical_name():
    assert split_physical_name(TABLE) == ("telecom", "customers")


def test_resolve_contract_table(contract):
    assert resolve_contract_table(contract) == TABLE
    assert resolve_contract_table(contract, override="other.table") == "other.table"
    assert resolve_contract_table(
        {"database_name": "retail", "table_name": "orders", "schema": []},
    ) == "retail.orders"


def test_load_contract_file(contract, tmp_path):
    path = tmp_path / "telecom.customers.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    loaded = load_contract_file(path)
    assert loaded["physicalName"] == TABLE


def test_push_file_dry_run(contract, tmp_path):
    path = tmp_path / "telecom.customers.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    svc = CatalogService(store=store, config=CatalogConfig())
    result = svc.push_file(path, dry_run=True)
    assert result.table == TABLE
    assert result.dry_run is True
    assert result.preview is not None


def test_push_batch_dry_run(contract, tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.dump(contract), encoding="utf-8")
    other = yaml.safe_load(yaml.dump(contract))
    other["physicalName"] = "telecom.orders"
    other["table_name"] = "orders"
    other["schema"][0]["physicalName"] = "telecom.orders"
    other["schema"][0]["name"] = "telecom_orders"
    (tmp_path / "b.yaml").write_text(yaml.dump(other), encoding="utf-8")
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    svc = CatalogService(store=store, config=CatalogConfig())
    results, errors = svc.push_batch(tmp_path, dry_run=True)
    assert errors == []
    assert len(results) == 2
    assert {r.table for r in results} == {TABLE, "telecom.orders"}


def test_openmetadata_plan_fqn(contract):
    plan = build_openmetadata_plan(contract, TABLE)
    assert plan.table_fqn == "redibis.telecom.default.customers"


def test_openmetadata_ui_url():
    assert openmetadata_ui_url(
        "http://localhost:8585", "redibis.telecom.default.customers",
    ) == "http://localhost:8585/table/redibis.telecom.default.customers"
    assert openmetadata_ui_url(
        "http://om:8585/api", "redibis.telecom.default.customers",
    ) == "http://om:8585/table/redibis.telecom.default.customers"


def test_pii_tags_on_columns(contract):
    plan = build_openmetadata_plan(contract, TABLE)
    cols = {c["name"]: c for c in plan.table_payload["columns"]}
    msisdn_tags = {t["tagFQN"] for t in cols["msisdn"]["tags"]}
    assert "PII.NonSensitive" in msisdn_tags
    assert "Redibis.PHONE_NUMBER" in msisdn_tags
    nid_tags = {t["tagFQN"] for t in cols["national_id"]["tags"]}
    assert "PII.Sensitive" in nid_tags
    # R1: Redibis-written tags are Automated, not Manual
    for tag in cols["msisdn"]["tags"]:
        assert tag["labelType"] == "Automated"
        assert tag["state"] == "Confirmed"


def test_glossary_per_database(contract):
    plan = build_openmetadata_plan(contract, TABLE)
    assert plan.glossary_terms
    for term in plan.glossary_terms:
        assert term["glossary"] == "Redibis_telecom"
        # name stays column name (unique within per-db glossary)
        assert term["name"] in {"msisdn", "national_id", "status"}


def test_already_exists_helper_no_bare_400():
    from redibis.services.catalog.openmetadata import _is_already_exists_error

    assert _is_already_exists_error(RuntimeError("… (409): Entity already exists"))
    assert _is_already_exists_error(RuntimeError("tag already exists"))
    assert not _is_already_exists_error(RuntimeError("OpenMetadata PUT (400): bad request"))


def test_publisher_dry_run(contract):
    pub = OpenMetadataPublisher(OpenMetadataSettings())
    result = pub.push(contract, TABLE, options=CatalogPushOptions(), dry_run=True)
    assert result.dry_run is True
    assert result.backend == "openmetadata"
    assert result.preview["table_fqn"] == "redibis.telecom.default.customers"
    assert "reconcile" in result.preview
    assert "diff" in result.preview["reconcile"]


def test_feedback_suppression_on_tag_delete(tmp_path):
    from datetime import datetime, timezone

    from redibis.services.catalog.assertions import (
        AssertionKey,
        Facet,
        LedgerEntry,
        value_hash,
    )
    from redibis.services.catalog.feedback import sync_feedback
    from redibis.store.catalog_ledger import CatalogLedgerStore, SuppressionStore
    from redibis.store.storage_backend import LocalBackend

    backend = LocalBackend(str(tmp_path / "store"))
    bucket = "contracts"
    ledger = CatalogLedgerStore(backend, bucket)
    suppressions = SuppressionStore(backend, bucket)
    key = AssertionKey(
        asset_fqn="hive.telecom.public.customers",
        column_path="msisdn",
        facet=Facet.PII_TAG,
        value_key="PII.Sensitive",
    )
    ledger.upsert_entries(
        TABLE,
        "openmetadata",
        [
            LedgerEntry(
                backend="openmetadata",
                key=key,
                value_hash=value_hash("PII.Sensitive"),
                scan_id="s1",
                published_at=datetime.now(timezone.utc),
            )
        ],
    )

    class FakeClient:
        def get_events(self, *, entity_type="table", timestamp_ms=0):
            return [{
                "eventType": "entityUpdated",
                "timestamp": 1_700_000_000_000,
                "userName": "alice",
                "entity": {"fullyQualifiedName": "hive.telecom.public.customers"},
                "changeDescription": {
                    "fieldsDeleted": [{
                        "name": "columns.msisdn.tags",
                        "oldValue": [{
                            "tagFQN": "PII.Sensitive",
                            "labelType": "Automated",
                        }],
                    }],
                },
            }]

    result = sync_feedback(
        FakeClient(),
        backend="openmetadata",
        table=TABLE,
        last_seen_ms=0,
        suppression_store=suppressions,
        ledger_store=ledger,
        storage=backend,
        bucket=bucket,
    )
    assert result["suppressions_written"] == 1
    assert result["ledger_dropped"] == 1
    assert suppressions.list(TABLE, "openmetadata")
    assert ledger.get_entries(TABLE, "openmetadata") == []



def test_golden_preview_snapshot(contract):
    plan = build_openmetadata_plan(contract, TABLE)
    preview = om_preview(plan, CatalogPushOptions())
    assert preview["backend"] == "openmetadata"
    assert preview["table_fqn"] == "redibis.telecom.default.customers"
    assert "policy_tags" in preview
    assert isinstance(preview["policy_tags"], list)
    targets = [s["target"] for s in preview["steps"]]
    assert "databaseService" in targets
    assert "table" in targets
    assert "dataContract" in targets
    if GOLDEN_OM.exists():
        expected = json.loads(GOLDEN_OM.read_text(encoding="utf-8"))
        # Allow additive keys (policy_tags) while locking core step payloads.
        assert preview["table_fqn"] == expected["table_fqn"]
        assert preview["backend"] == expected["backend"]
        # Compare non-policy steps by target
        core_targets = {"databaseService", "database", "databaseSchema", "table",
                        "glossaryTerms", "dataContract"}
        got = {s["target"]: s for s in preview["steps"] if s["target"] in core_targets}
        exp = {s["target"]: s for s in expected["steps"] if s["target"] in core_targets}
        assert set(got) == set(exp)
        for key in ("databaseService", "database", "databaseSchema"):
            assert got[key]["body"] == exp[key]["body"]


def test_policy_tags_on_columns(contract):
    from redibis.classification.models import ClassificationResult, ResolvedTag

    results = [
        ClassificationResult(
            table=TABLE,
            column="msisdn",
            resolved_tags=[
                ResolvedTag(domain="DataSensitivity", tag="PII"),
                ResolvedTag(domain="RegulatoryCompliance", tag="LawfulIntercept"),
            ],
        ),
        ClassificationResult(
            table=TABLE,
            column="national_id",
            resolved_tags=[ResolvedTag(domain="DataSensitivity", tag="Sensitive")],
        ),
    ]
    plan = build_openmetadata_plan(
        contract, TABLE,
        options=CatalogPushOptions(classification_results=results),
    )
    cols = {c["name"]: c for c in plan.table_payload["columns"]}
    msisdn_fqns = {t["tagFQN"] for t in cols["msisdn"]["tags"]}
    assert "RedibisPolicy.DataSensitivity__PII" in msisdn_fqns
    assert not any("LawfulIntercept" in t for t in msisdn_fqns)
    nid_fqns = {t["tagFQN"] for t in cols["national_id"]["tags"]}
    assert "RedibisPolicy.DataSensitivity__Sensitive" in nid_fqns

    preview = om_preview(plan, CatalogPushOptions(classification_results=results))
    assert "RedibisPolicy.DataSensitivity__PII" in preview["policy_tags"]
    assert any(s["target"] == "classification" for s in preview["steps"])
    assert any(s["target"] == "tag" for s in preview["steps"])


def test_preview_ensures_redibis_entity_tags(contract):
    """Entity / contract tags under Redibis.* must be created before table PUT."""
    enriched = json.loads(json.dumps(contract))
    for prop in enriched["schema"][0]["properties"]:
        if prop["name"] == "msisdn":
            tags = list(prop.get("tags") or [])
            if "demo" not in tags:
                tags.append("demo")
            prop["tags"] = tags
            break
    plan = build_openmetadata_plan(enriched, TABLE)
    preview = om_preview(plan, CatalogPushOptions())
    class_steps = [s for s in preview["steps"] if s["target"] == "classification"]
    tag_steps = [s for s in preview["steps"] if s["target"] == "tag"]
    class_names = {s["body"]["name"] for s in class_steps}
    assert "Redibis" in class_names
    tag_names = {s["body"]["name"] for s in tag_steps}
    assert "PHONE_NUMBER" in tag_names or "demo" in tag_names
    assert "demo" in tag_names
    assert any(s["body"].get("classification") == "Redibis" for s in tag_steps)


def test_catalog_service_classifies_when_enabled(contract, tmp_path):
    from redibis.config import ClassificationConfig, RedibisConfig

    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    store.upsert(contract, table=TABLE, workflow="manual")
    cfg = RedibisConfig.default()
    cfg.classification = ClassificationConfig(enabled=True, policy_pack="telecom")
    svc = CatalogService.from_redibis_config(store, cfg)
    result = svc.push(TABLE, dry_run=True)
    assert result.dry_run is True
    # Policy tags may be empty for telecom pack on fixture columns; preview must exist
    assert result.preview is not None
    assert "policy_tags" in result.preview


def test_business_name_as_display_name(contract):
    # Inject a businessName and ensure OM column displayName is set
    enriched = json.loads(json.dumps(contract))
    for prop in enriched["schema"][0]["properties"]:
        if prop["name"] == "msisdn":
            prop["businessName"] = "Customer Mobile"
            prop["description"] = "Primary contact mobile for the subscriber."
            break
    plan = build_openmetadata_plan(enriched, TABLE)
    cols = {c["name"]: c for c in plan.table_payload["columns"]}
    assert cols["msisdn"]["displayName"] == "Customer Mobile"
    glossary_names = {t["displayName"] for t in plan.glossary_terms}
    assert "Customer Mobile" in glossary_names


def test_business_definition_maps_to_om_description(contract):
    """Enriched business.definition becomes OM column description when description is absent."""
    enriched = json.loads(json.dumps(contract))
    for prop in enriched["schema"][0]["properties"]:
        if prop["name"] == "status":
            prop.pop("description", None)
            prop["businessName"] = "Account Status"
            prop["business"] = {
                "definition": "Lifecycle state for the subscriber account.",
                "synonyms": ["status"],
                "tags": ["tutorial"],
            }
            break
    plan = build_openmetadata_plan(enriched, TABLE)
    cols = {c["name"]: c for c in plan.table_payload["columns"]}
    assert "Lifecycle state for the subscriber account." in cols["status"]["description"]
    assert cols["status"]["displayName"] == "Account Status"


def test_top_level_description_wins_over_business_definition(contract):
    enriched = json.loads(json.dumps(contract))
    for prop in enriched["schema"][0]["properties"]:
        if prop["name"] == "status":
            prop["description"] = "Explicit top-level description."
            prop["business"] = {
                "definition": "Should not replace the top-level description.",
            }
            break
    plan = build_openmetadata_plan(enriched, TABLE)
    cols = {c["name"]: c for c in plan.table_payload["columns"]}
    assert cols["status"]["description"].startswith("Explicit top-level description.")
    assert "Should not replace" not in cols["status"]["description"]


def test_missing_jwt_raises(contract, monkeypatch):
    monkeypatch.delenv("OM_BOT_JWT", raising=False)
    pub = OpenMetadataPublisher(OpenMetadataSettings(jwt=""))
    with pytest.raises(Exception):
        pub.push(contract, TABLE, options=CatalogPushOptions(), dry_run=False)


def test_placeholder_jwt_raises_clear_error(monkeypatch):
    from redibis.config import ConfigError
    from redibis.services.catalog.openmetadata import _validate_om_jwt

    with pytest.raises(ConfigError, match="placeholder"):
        _validate_om_jwt("…")
    with pytest.raises(ConfigError, match="non-ASCII|placeholder"):
        _validate_om_jwt("Bearer…token")
    assert _validate_om_jwt("eyJhbGciOiJIUzI1NiJ9.e30.sig").startswith("eyJ")


def test_atlas_plan_qualified_name(contract):
    plan = build_atlas_plan(contract, TABLE)
    assert plan.entity_qualified_name == "telecom.customers@redibis"
    assert "PII.Sensitive" in plan.classifications
    assert "PII.NonSensitive" in plan.classifications


def test_atlas_publisher_dry_run(contract):
    pub = AtlasPublisher(AtlasSettings(password="x"))
    result = pub.push(contract, TABLE, options=CatalogPushOptions(), dry_run=True)
    assert result.backend == "atlas"
    assert result.preview["backend"] == "atlas"
    assert result.preview["steps"][1]["target"] == "hive_table"


def test_datahub_dataset_urn():
    assert dataset_urn("redibis", TABLE, "PROD") == (
        "urn:li:dataset:(urn:li:dataPlatform:redibis,telecom.customers,PROD)"
    )


def test_datahub_plan_schema_fields(contract):
    plan = build_datahub_plan(contract, TABLE)
    fields = plan.upsert_payload[1]["aspect"]["fields"]
    by_name = {f["fieldPath"]: f for f in fields}
    assert set(by_name) == {"msisdn", "national_id", "status"}
    msisdn_tags = {t["tag"] for t in by_name["msisdn"]["globalTags"]["tags"]}
    assert "urn:li:tag:PII.NonSensitive" in msisdn_tags


def test_datahub_publisher_dry_run(contract):
    pub = DataHubPublisher(DataHubSettings(token="x"))
    result = pub.push(contract, TABLE, options=CatalogPushOptions(), dry_run=True)
    assert result.backend == "datahub"
    assert "dataset_urn" in result.preview


def test_get_publisher_atlas():
    pub = get_publisher(CatalogConfig(backend="atlas"))
    assert pub.backend == "atlas"


def test_get_publisher_datahub():
    pub = get_publisher(CatalogConfig(backend="datahub"))
    assert pub.backend == "datahub"


def test_record_catalog_push_telemetry(tmp_path):
    backend = LocalBackend(str(tmp_path))
    meta = ContractMetadataStore(backend, "contracts")
    meta.record_catalog_push(
        TABLE,
        backend="openmetadata",
        entity_fqn="redibis.telecom.default.customers",
        contract_version="1.0.0",
        details={"entity_id": "abc"},
    )
    tel = meta.get_telemetry(TABLE)
    assert tel["catalog"]["openmetadata"]["entity_fqn"] == "redibis.telecom.default.customers"
    assert tel["last_catalog_push"]["backend"] == "openmetadata"


def test_table_semantics(contract):
    meta = table_semantics(contract, TABLE)
    assert "Customer master" in meta.description


def test_table_semantics_accepts_structured_contract_description(contract):
    contract["description"] = {
        "description": "Customer master from structured metadata",
        "purpose": "Customer operations",
    }
    meta = table_semantics(contract, TABLE)
    assert meta.description == "Customer master from structured metadata"
