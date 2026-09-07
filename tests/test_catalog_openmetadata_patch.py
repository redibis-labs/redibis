"""OpenMetadata JSON Patch builder + 409 retry (mocked client)."""

from __future__ import annotations

from redibis.services.catalog.assertions import (
    AssertionKey,
    Facet,
    LedgerEntry,
    value_hash,
)
from redibis.services.catalog.openmetadata import (
    OpenMetadataConflictError,
    OpenMetadataPublisher,
    OpenMetadataSettings,
    _is_already_exists_error,
)
from redibis.services.catalog.openmetadata_patch import (
    build_json_patch,
    remote_assertions_from_table,
)
from redibis.services.catalog.reconcile import IntentOp
from redibis.services.catalog.base import CatalogPushOptions


ASSET = "hive_prod.telecom.public.customers"


def _entity_snapshot() -> dict:
    return {
        "fullyQualifiedName": ASSET,
        "id": "table-1",
        "description": "Existing steward description",
        "columns": [
            {
                "name": "msisdn",
                "dataType": "VARCHAR",
                "description": "Human-written MSISDN desc",
                "tags": [
                    {
                        "tagFQN": "PII.NonSensitive",
                        "labelType": "Manual",
                        "source": "Classification",
                        "state": "Confirmed",
                    },
                    {
                        "tagFQN": "Tier.Gold",
                        "labelType": "Manual",
                        "source": "Classification",
                        "state": "Confirmed",
                    },
                ],
            },
            {
                "name": "email",
                "dataType": "VARCHAR",
                "description": "",
                "tags": [],
            },
        ],
        "owners": [{"name": "alice"}],
        "domain": "Customer",
    }


def test_build_json_patch_only_redibis_paths():
    entity = _entity_snapshot()
    ops = [
        IntentOp(
            op="add",
            key=AssertionKey(ASSET, "email", Facet.ENTITY_TAG, "Redibis.EMAIL"),
            value="Redibis.EMAIL",
            state="confirmed",
        ),
        IntentOp(
            op="remove",
            key=AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.NonSensitive"),
            prior="PII.NonSensitive",
        ),
        IntentOp(
            op="update",
            key=AssertionKey(ASSET, "email", Facet.COLUMN_DESCRIPTION, ""),
            value="Contact email",
            state="confirmed",
        ),
    ]
    patch = build_json_patch(ops, entity)
    paths = [p["path"] for p in patch]
    assert all("dataType" not in path for path in paths)
    assert all("owners" not in path for path in paths)
    assert all("domain" not in path for path in paths)
    assert all("tier" not in path.lower() for path in paths)
    # Atomic column-tags replace (no indexed /tags/N arithmetic)
    assert any(p.endswith("/tags") and not p.endswith("/tags/-") for p in paths)
    assert not any("/tags/" in p and p.rstrip("/").split("/")[-1].isdigit() for p in paths)
    assert any(p.endswith("/description") for p in paths)
    msisdn_tags = next(p for p in patch if p["path"] == "/columns/0/tags")
    assert msisdn_tags["op"] == "replace"
    fqns = [t["tagFQN"] for t in msisdn_tags["value"]]
    assert "Tier.Gold" in fqns  # foreign preserved
    assert "PII.NonSensitive" not in fqns


def test_build_json_patch_atomic_two_removes_same_column():
    """Two Redibis tag removals → one replace; no index-shift hazard."""
    entity = _entity_snapshot()
    entity["columns"][0]["tags"] = [
        {
            "tagFQN": "PII.Sensitive",
            "labelType": "Automated",
            "source": "Classification",
            "state": "Confirmed",
        },
        {
            "tagFQN": "Tier.Gold",
            "labelType": "Manual",
            "source": "Classification",
            "state": "Confirmed",
        },
        {
            "tagFQN": "Redibis.EMAIL",
            "labelType": "Automated",
            "source": "Classification",
            "state": "Confirmed",
        },
    ]
    ops = [
        IntentOp(
            op="remove",
            key=AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.Sensitive"),
            prior="PII.Sensitive",
        ),
        IntentOp(
            op="remove",
            key=AssertionKey(ASSET, "msisdn", Facet.ENTITY_TAG, "Redibis.EMAIL"),
            prior="Redibis.EMAIL",
        ),
    ]
    patch = build_json_patch(ops, entity)
    tag_ops = [p for p in patch if p["path"].endswith("/tags")]
    assert len(tag_ops) == 1
    assert tag_ops[0]["op"] == "replace"
    assert tag_ops[0]["path"] == "/columns/0/tags"
    fqns = [t["tagFQN"] for t in tag_ops[0]["value"]]
    assert fqns == ["Tier.Gold"]
    # Never emit indexed remove ops
    assert not any(p.get("op") == "remove" for p in patch)
    assert not any(
        "/tags/" in p["path"] and p["path"].rstrip("/").split("/")[-1].isdigit()
        for p in patch
    )


def test_build_json_patch_remove_and_add_same_column():
    """remove + add on one column → one replace with the added tag only."""
    entity = _entity_snapshot()
    entity["columns"][0]["tags"] = [
        {
            "tagFQN": "PII.NonSensitive",
            "labelType": "Automated",
            "source": "Classification",
            "state": "Confirmed",
        },
        {
            "tagFQN": "PersonalData.Sensitive",
            "labelType": "Manual",
            "source": "Classification",
            "state": "Confirmed",
        },
    ]
    ops = [
        IntentOp(
            op="remove",
            key=AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.NonSensitive"),
            prior="PII.NonSensitive",
        ),
        IntentOp(
            op="add",
            key=AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.Sensitive"),
            value="PII.Sensitive",
            state="confirmed",
        ),
    ]
    patch = build_json_patch(ops, entity)
    tag_ops = [p for p in patch if p["path"].endswith("/tags")]
    assert len(tag_ops) == 1
    assert tag_ops[0]["op"] == "replace"
    fqns = [t["tagFQN"] for t in tag_ops[0]["value"]]
    assert "PII.Sensitive" in fqns
    assert "PII.NonSensitive" not in fqns
    # Foreign tags survive in original form
    assert "PersonalData.Sensitive" in fqns
    personal = next(t for t in tag_ops[0]["value"] if t["tagFQN"] == "PersonalData.Sensitive")
    assert personal["labelType"] == "Manual"


def test_build_json_patch_two_columns_two_replaces():
    """Ops on two columns → two replace ops at the correct indexes."""
    entity = _entity_snapshot()
    entity["columns"][0]["tags"] = [
        {
            "tagFQN": "PII.Sensitive",
            "labelType": "Automated",
            "source": "Classification",
            "state": "Confirmed",
        },
    ]
    entity["columns"][1]["tags"] = []
    ops = [
        IntentOp(
            op="remove",
            key=AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.Sensitive"),
            prior="PII.Sensitive",
        ),
        IntentOp(
            op="add",
            key=AssertionKey(ASSET, "email", Facet.ENTITY_TAG, "Redibis.EMAIL"),
            value="Redibis.EMAIL",
            state="confirmed",
        ),
    ]
    patch = build_json_patch(ops, entity)
    tag_ops = [p for p in patch if p["path"].endswith("/tags")]
    assert len(tag_ops) == 2
    paths = {p["path"] for p in tag_ops}
    assert paths == {"/columns/0/tags", "/columns/1/tags"}
    assert all(p["op"] == "replace" for p in tag_ops)
    by_path = {p["path"]: p for p in tag_ops}
    assert "PII.Sensitive" not in [t["tagFQN"] for t in by_path["/columns/0/tags"]["value"]]
    assert "Redibis.EMAIL" in [t["tagFQN"] for t in by_path["/columns/1/tags"]["value"]]


def test_remote_assertions_authority_mapping():
    entity = _entity_snapshot()
    # Ledger matches Automated Redibis tag hash
    key = AssertionKey(ASSET, "email", Facet.ENTITY_TAG, "Redibis.EMAIL")
    # Add an Automated tag to entity
    entity["columns"][1]["tags"] = [
        {
            "tagFQN": "Redibis.EMAIL",
            "labelType": "Automated",
            "source": "Classification",
            "state": "Confirmed",
        }
    ]
    ledger = [
        LedgerEntry(
            backend="openmetadata",
            key=key,
            value_hash=value_hash("Redibis.EMAIL"),
            scan_id="s1",
            published_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
        )
    ]
    remote = remote_assertions_from_table(entity, ledger_entries=ledger)
    by_key = {a.key: a for a in remote}
    assert by_key[key].authority.value == "redibis"
    # Manual PII on msisdn → human
    human_key = AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.NonSensitive")
    assert by_key[human_key].authority.value == "human"
    # Non-empty unmatched description → human
    desc_key = AssertionKey(ASSET, "msisdn", Facet.COLUMN_DESCRIPTION, "")
    assert by_key[desc_key].authority.value == "human"


def test_already_exists_helper():
    assert _is_already_exists_error(RuntimeError("OpenMetadata PUT /v1/tags (409): conflict"))
    assert _is_already_exists_error(RuntimeError("Entity already exists"))
    assert _is_already_exists_error(RuntimeError("tag already exists in classification"))
    assert not _is_already_exists_error(
        RuntimeError("OpenMetadata PUT /v1/tags (400): Invalid tag name")
    )
    assert not _is_already_exists_error(RuntimeError("OpenMetadata GET (404): not found"))


def test_build_json_patch_glossary_link_as_taglabel():
    """GLOSSARY_LINK ops emit Glossary-sourced TagLabels in the atomic replace."""
    entity = _entity_snapshot()
    ops = [
        IntentOp(
            op="add",
            key=AssertionKey(
                ASSET, "msisdn", Facet.GLOSSARY_LINK, "Redibis_telecom.msisdn",
            ),
            value="Customer Mobile",
            state="confirmed",
        ),
    ]
    patch = build_json_patch(ops, entity)
    tag_ops = [p for p in patch if p["path"] == "/columns/0/tags"]
    assert len(tag_ops) == 1
    gloss = next(
        t for t in tag_ops[0]["value"] if t["tagFQN"] == "Redibis_telecom.msisdn"
    )
    assert gloss["source"] == "Glossary"
    assert gloss["labelType"] == "Automated"
    # Foreign Tier.Gold preserved
    assert any(t["tagFQN"] == "Tier.Gold" for t in tag_ops[0]["value"])


def test_glossary_put_failure_skips_ledger_entry(tmp_path):
    """GLOSSARY_LINK add whose term PUT fails → no ledger entry for that key."""
    from redibis.store.catalog_ledger import CatalogLedgerStore
    from redibis.store.storage_backend import LocalBackend

    settings = OpenMetadataSettings(
        entity_mode="enrich_existing",
        jwt="eyJhbGciOiJIUzI1NiJ9.e30.sig",
    )
    storage = LocalBackend(str(tmp_path / "store"))
    ledger_store = CatalogLedgerStore(storage, "contracts")
    pub = OpenMetadataPublisher(settings, ledger_store=ledger_store)

    entity = _entity_snapshot()
    term_key = AssertionKey(
        ASSET, "msisdn", Facet.GLOSSARY_LINK, "Redibis_telecom.msisdn",
    )

    class FakeClient:
        def get_table(self, fqn, *, fields=""):
            return entity

        def ensure_tag_steps(self, steps):
            return 0

        def patch_table(self, fqn, patch):
            return {**entity, "id": "table-1"}

        def upsert_glossary_term(self, term):
            raise RuntimeError("OpenMetadata PUT /v1/glossaryTerms (500): boom")

        def upsert_odcs_contract(self, plan, table_id):
            return {"id": "c1"}

    contract = {
        "physicalName": "telecom.customers",
        "description": "Customers",
        "schema": [{
            "name": "customers",
            "physicalName": "telecom.customers",
            "properties": [
                {
                    "name": "msisdn",
                    "logicalType": "string",
                    "description": "Mobile",
                    "businessName": "Customer Mobile",
                },
                {"name": "email", "logicalType": "string", "description": ""},
            ],
        }],
    }

    try:
        pub._push_enrich(
            FakeClient(),  # type: ignore[arg-type]
            contract,
            "telecom.customers",
            target_fqn=ASSET,
            plan=pub._plan(
                contract, "telecom.customers",
                CatalogPushOptions(tags=False, glossary=True, contract=False),
            ),
            options=CatalogPushOptions(tags=False, glossary=True, contract=False),
        )
        raise AssertionError("expected glossary PUT failure to propagate")
    except RuntimeError as exc:
        assert "glossaryTerms" in str(exc) or "boom" in str(exc)

    entries = ledger_store.get_entries("telecom.customers", "openmetadata")
    assert all(e.key != term_key for e in entries)
    assert not any(e.key.facet is Facet.GLOSSARY_LINK for e in entries)
    """On 409, re-GET + re-reconcile; never resend the stale patch."""
    settings = OpenMetadataSettings(
        entity_mode="enrich_existing",
        jwt="eyJhbGciOiJIUzI1NiJ9.e30.sig",
    )
    pub = OpenMetadataPublisher(settings)

    entity_v1 = _entity_snapshot()
    entity_v2 = _entity_snapshot()
    entity_v2["columns"][1]["tags"] = [
        {"tagFQN": "Other.Tag", "labelType": "Manual", "source": "Classification"}
    ]

    patches_seen: list[list[dict]] = []

    class FakeClient:
        def __init__(self):
            self.gets = 0
            self.patches = 0

        def get_table(self, fqn, *, fields=""):
            self.gets += 1
            return entity_v1 if self.gets == 1 else entity_v2

        def ensure_tag_steps(self, steps):
            return 0

        def patch_table(self, fqn, patch):
            self.patches += 1
            patches_seen.append(list(patch))
            if self.patches == 1:
                raise OpenMetadataConflictError("OpenMetadata PATCH (409): version conflict")
            return {**entity_v2, "id": "table-1"}

        def upsert_glossary_terms(self, plan):
            return 0

        def upsert_glossary_term(self, term):
            return True

        def upsert_odcs_contract(self, plan, table_id):
            return {"id": "c1"}

    client = FakeClient()
    contract = {
        "physicalName": "telecom.customers",
        "description": "x",
        "schema": [{
            "name": "customers",
            "physicalName": "telecom.customers",
            "properties": [
                {"name": "msisdn", "logicalType": "string", "description": "m"},
                {
                    "name": "email",
                    "logicalType": "string",
                    "description": "Contact email",
                    "privacy": {
                        "classification_engine": {
                            "detected": True,
                            "entity_type": "EMAIL",
                            "confidence": 0.9,
                        }
                    },
                    "tags": [],
                },
            ],
        }],
    }

    # Monkeypatch build path uses FakeClient directly via _push_enrich
    result = pub._push_enrich(
        client,  # type: ignore[arg-type]
        contract,
        "telecom.customers",
        target_fqn=ASSET,
        plan=pub._plan(contract, "telecom.customers", CatalogPushOptions()),
        options=CatalogPushOptions(tags=True, glossary=False, contract=False),
    )
    assert client.gets >= 2
    assert client.patches == 2
    assert len(patches_seen) == 2
    # Second patch rebuilt against entity_v2 — must preserve Other.Tag on email.
    email_tags_v2 = next(
        (p for p in patches_seen[1] if p.get("path") == "/columns/1/tags"),
        None,
    )
    assert email_tags_v2 is not None
    fqns_v2 = [t["tagFQN"] for t in email_tags_v2["value"]]
    assert "Other.Tag" in fqns_v2
    assert patches_seen[0] != patches_seen[1]
    assert result.entity_fqn == ASSET
    assert result.details.get("write_mode") == "patch"
