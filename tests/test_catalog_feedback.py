"""Catalog feedback → suppressions (human wins across scans)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from redibis.services.catalog.assertions import (
    Assertion,
    AssertionKey,
    Authority,
    CoverageRecord,
    CoverageStatus,
    Facet,
    LedgerEntry,
    value_hash,
)
from redibis.services.catalog.feedback import (
    _deleted_tags,
    _table_from_fqn,
    load_feedback_cursor,
    sync_feedback,
)
from redibis.services.catalog.reconcile import ReconcileMode, reconcile
from redibis.store.catalog_ledger import CatalogLedgerStore, SuppressionStore
from redibis.store.storage_backend import LocalBackend


ASSET = "hive_prod.telecom.public.customers"
TABLE = "telecom.customers"
BACKEND = "openmetadata"
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_table_from_fqn_four_part():
    assert _table_from_fqn(ASSET) == "telecom.customers"


def test_feedback_deleted_tag_creates_suppression(tmp_path):
    """entityUpdated fieldsDeleted columns.msisdn.tags / PII.Sensitive → suppression + ledger drop."""
    backend = LocalBackend(str(tmp_path / "store"))
    bucket = "contracts"
    suppression_store = SuppressionStore(backend, bucket)
    ledger_store = CatalogLedgerStore(backend, bucket)

    key = AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.Sensitive")
    ledger_store.upsert_entries(
        TABLE,
        BACKEND,
        [
            LedgerEntry(
                backend=BACKEND,
                key=key,
                value_hash=value_hash("PII.Sensitive"),
                scan_id="s0",
                published_at=NOW,
            )
        ],
    )

    class FakeClient:
        def get_events(self, *, entity_type="table", timestamp_ms=0):
            return [
                {
                    "eventType": "entityUpdated",
                    "timestamp": 1_700_000_000_100,
                    "userName": "alice",
                    "entity": {"fullyQualifiedName": ASSET},
                    "changeDescription": {
                        "fieldsDeleted": [
                            {
                                "name": "columns.msisdn.tags",
                                "oldValue": [
                                    {
                                        "tagFQN": "PII.Sensitive",
                                        "labelType": "Automated",
                                        "source": "Classification",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ]

    result = sync_feedback(
        FakeClient(),
        backend=BACKEND,
        table=TABLE,
        suppression_store=suppression_store,
        ledger_store=ledger_store,
        storage=backend,
        bucket=bucket,
    )
    assert result["suppressions_written"] == 1
    assert result["ledger_dropped"] >= 1
    suppressions = suppression_store.list(TABLE, BACKEND)
    assert len(suppressions) == 1
    assert suppressions[0].key.value_key == "PII.Sensitive"
    assert suppressions[0].actor == "alice"
    assert suppressions[0].source == "om_change_event"
    assert ledger_store.get_entries(TABLE, BACKEND) == []


def test_feedback_suppression_blocks_next_push_reconcile(tmp_path):
    """After feedback, reconciler does not re-add the suppressed tag."""
    backend = LocalBackend(str(tmp_path / "store"))
    bucket = "contracts"
    suppression_store = SuppressionStore(backend, bucket)
    ledger_store = CatalogLedgerStore(backend, bucket)

    key = AssertionKey(ASSET, "msisdn", Facet.PII_TAG, "PII.Sensitive")

    class FakeClient:
        def get_events(self, *, entity_type="table", timestamp_ms=0):
            return [
                {
                    "eventType": "entityUpdated",
                    "timestamp": 1_700_000_000_200,
                    "userName": "bob",
                    "entity": {"fullyQualifiedName": ASSET},
                    "changeDescription": {
                        "fieldsDeleted": [
                            {
                                "name": "columns.msisdn.tags",
                                "oldValue": {"tagFQN": "PII.Sensitive"},
                            }
                        ]
                    },
                }
            ]

    sync_feedback(
        FakeClient(),
        backend=BACKEND,
        suppression_store=suppression_store,
        ledger_store=ledger_store,
        storage=backend,
        bucket=bucket,
    )

    coverage = [
        CoverageRecord(
            scan_id="scan-next",
            asset_fqn=ASSET,
            column_path="msisdn",
            facet=Facet.PII_TAG,
            status=CoverageStatus.EVALUATED,
        )
    ]
    desired = [
        Assertion(
            key=key,
            value="PII.Sensitive",
            authority=Authority.REDIBIS,
            confidence=0.97,
            scan_id="scan-next",
            observed_at=NOW,
        )
    ]
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=desired,
        remote=[],
        ledger=[],
        coverage=coverage,
        suppressions=suppression_store.list(TABLE, BACKEND),
        mode=ReconcileMode.NORMAL,
        now=NOW,
    )
    assert plan.ops == []
    assert plan.conflicts == []


def test_deleted_tags_old_value_json_string_and_list():
    """oldValue as JSON string and as list both parse."""
    as_list = _deleted_tags({
        "fieldsDeleted": [
            {
                "name": "columns.msisdn.tags",
                "oldValue": [{"tagFQN": "PII.Sensitive"}],
            }
        ]
    })
    assert as_list == [("msisdn", "PII.Sensitive")]

    as_string = _deleted_tags({
        "fieldsDeleted": [
            {
                "name": "columns.email.tags",
                "oldValue": json.dumps({"tagFQN": "Redibis.EMAIL", "labelType": "Automated"}),
            }
        ]
    })
    assert as_string == [("email", "Redibis.EMAIL")]

    as_list_string = _deleted_tags({
        "fieldsDeleted": [
            {
                "name": "columns.msisdn.tags",
                "oldValue": json.dumps([{"tagFQN": "PII.NonSensitive"}]),
            }
        ]
    })
    assert as_list_string == [("msisdn", "PII.NonSensitive")]


def test_feedback_non_redibis_tag_no_suppression(tmp_path):
    """Deleted Tier.Gold (not Redibis-owned) → no suppression."""
    backend = LocalBackend(str(tmp_path / "store"))
    bucket = "contracts"
    suppression_store = SuppressionStore(backend, bucket)

    class FakeClient:
        def get_events(self, *, entity_type="table", timestamp_ms=0):
            return [
                {
                    "eventType": "entityUpdated",
                    "timestamp": 1_700_000_000_300,
                    "userName": "carol",
                    "entity": {"fullyQualifiedName": ASSET},
                    "changeDescription": {
                        "fieldsDeleted": [
                            {
                                "name": "columns.msisdn.tags",
                                "oldValue": [
                                    {
                                        "tagFQN": "Tier.Gold",
                                        "labelType": "Manual",
                                        "source": "Classification",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ]

    result = sync_feedback(
        FakeClient(),
        backend=BACKEND,
        table=TABLE,
        suppression_store=suppression_store,
        storage=backend,
        bucket=bucket,
    )
    assert result["suppressions_written"] == 0
    assert suppression_store.list(TABLE, BACKEND) == []


def test_feedback_cursor_advances_to_max_ts(tmp_path):
    """Cursor advances to max_ts and is persisted."""
    backend = LocalBackend(str(tmp_path / "store"))
    bucket = "contracts"
    suppression_store = SuppressionStore(backend, bucket)

    class FakeClient:
        def get_events(self, *, entity_type="table", timestamp_ms=0):
            return [
                {
                    "eventType": "entityUpdated",
                    "timestamp": 1_700_000_000_050,
                    "userName": "dave",
                    "entity": {"fullyQualifiedName": ASSET},
                    "changeDescription": {"fieldsDeleted": []},
                },
                {
                    "eventType": "entityUpdated",
                    "timestamp": 1_700_000_000_999,
                    "userName": "dave",
                    "entity": {"fullyQualifiedName": ASSET},
                    "changeDescription": {
                        "fieldsDeleted": [
                            {
                                "name": "columns.msisdn.tags",
                                "oldValue": {"tagFQN": "PII.Sensitive"},
                            }
                        ]
                    },
                },
            ]

    result = sync_feedback(
        FakeClient(),
        backend=BACKEND,
        table=TABLE,
        suppression_store=suppression_store,
        storage=backend,
        bucket=bucket,
        last_seen_ms=0,
    )
    assert result["last_seen_ms"] == 1_700_000_000_999
    assert load_feedback_cursor(backend, bucket, BACKEND) == 1_700_000_000_999
