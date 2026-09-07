"""API tests for Final Review routes (/api/contracts/{table}/review)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


TABLE = "telecom.customers"


def _seed_contract(store: ContractStore) -> None:
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "telecom_customers_contract",
        "version": "1.0.0",
        "status": "active",
        "contract_uuid": "uuid-review-test",
        "schema": [{
            "name": "telecom_customers",
            "physicalName": TABLE,
            "properties": [
                {
                    "name": "email",
                    "logicalType": "string",
                    "tags": ["pii"],
                    "description": "Customer email",
                    "privacy": {"classification": "Restricted"},
                },
                {
                    "name": "city",
                    "logicalType": "string",
                    "description": "City name",
                },
            ],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="manual", run_id="r1")


@pytest.fixture
def review_client(tmp_path, monkeypatch):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    _seed_contract(store)

    from redibis.webapp import backend as web

    monkeypatch.setattr(web, "get_contract_store", lambda: store)
    return TestClient(web.app), store


def test_get_review_assembles_columns(review_client):
    client, _store = review_client
    r = client.get(f"/api/contracts/{TABLE}/review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == TABLE
    assert body["total_columns"] == 2
    assert body["approved_count"] == 0
    assert body["fully_approved"] is False
    names = {c["column"] for c in body["columns"]}
    assert names == {"email", "city"}
    assert all(c["review"]["status"] == "pending" for c in body["columns"])


def test_approve_column_updates_status(review_client):
    client, _store = review_client
    r = client.post(
        f"/api/contracts/{TABLE}/review/columns/email/approve",
        json={
            "reviewer": "alice",
            "approved": {
                "pii_flag": True,
                "tags": ["pii"],
                "classification": "Restricted",
                "definition": "Customer email",
                "glossary": [],
                "profiling_features": [],
            },
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved_count"] == 1
    email = next(c for c in body["columns"] if c["column"] == "email")
    assert email["review"]["status"] == "approved"
    assert email["review"]["reviewed_by"] == "alice"


def test_finalize_when_all_columns_done(review_client):
    client, _store = review_client
    for col in ("email", "city"):
        resp = client.post(
            f"/api/contracts/{TABLE}/review/columns/{col}/approve",
            json={"reviewer": "bob"},
        )
        assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["approved_count"] == 2
    assert body["fully_approved"] is True

    fin = client.post(f"/api/contracts/{TABLE}/review/finalize", json={"reviewer": "bob"})
    assert fin.status_code == 200, fin.text
    assert fin.json()["fully_approved"] is True


def test_review_page_route(review_client):
    client, _store = review_client
    r = client.get(f"/review?table={TABLE}")
    assert r.status_code == 200


def test_review_unknown_table_404(review_client):
    client, _store = review_client
    r = client.get("/api/contracts/missing.t/review")
    assert r.status_code == 404


def test_reviewer_required_on_approve(review_client):
    client, _store = review_client
    r = client.post(
        f"/api/contracts/{TABLE}/review/columns/email/approve",
        json={"reviewer": "", "approved": {"pii_flag": True, "tags": []}},
    )
    assert r.status_code == 400
    assert "reviewer" in r.json()["detail"].lower()


def test_column_audit_persisted_in_minio(review_client):
    client, store = review_client
    r = client.post(
        f"/api/contracts/{TABLE}/review/columns/email/approve",
        json={"reviewer": "alice", "note": "looks good"},
    )
    assert r.status_code == 200, r.text

    checkpoint = store.backend.get_json(
        store.bucket, f"_meta/reviews/{TABLE}.json",
    )
    assert checkpoint["columns"]["email"]["reviewed_by"] == "alice"
    assert checkpoint["columns"]["email"]["reviewed_at"]

    col_telemetry = store.metadata.get_column_telemetry(TABLE)
    assert col_telemetry["email"]["final_review"]["reviewed_by"] == "alice"
    assert col_telemetry["email"]["final_review"]["status"] == "approved"


def test_finalize_stamps_contract_level_audit(review_client):
    client, store = review_client
    for col in ("email", "city"):
        client.post(
            f"/api/contracts/{TABLE}/review/columns/{col}/approve",
            json={"reviewer": "carol"},
        )
    fin = client.post(f"/api/contracts/{TABLE}/review/finalize", json={"reviewer": "carol"})
    assert fin.status_code == 200, fin.text

    telemetry = store.metadata.get_telemetry(TABLE)
    assert telemetry["final_review"]["finalized_by"] == "carol"
    assert telemetry["final_review"]["fully_approved"] is True

    pkg = store.export_integration_package(TABLE)
    assert pkg["column_telemetry"]["email"]["final_review"]["reviewed_by"] == "carol"


def test_review_reflects_business_definition(review_client):
    client, store = review_client
    store.patch_definitions(
        TABLE,
        column_patches={
            "city": {
                "business": {"definition": "Municipality where the customer resides"},
                "tags": ["geo", "location"],
            },
        },
        decided_by="steward",
    )
    r = client.get(f"/api/contracts/{TABLE}/review")
    assert r.status_code == 200, r.text
    city = next(c for c in r.json()["columns"] if c["column"] == "city")
    assert city["definition"] == "Municipality where the customer resides"
    assert "geo" in city["tags"]


def test_stale_approval_reset_after_contract_edit(review_client):
    client, store = review_client
    approve = client.post(
        f"/api/contracts/{TABLE}/review/columns/email/approve",
        json={"reviewer": "alice"},
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["approved_count"] == 1

    store.patch_definitions(
        TABLE,
        column_patches={"email": {"description": "Updated email address"}},
        decided_by="steward",
    )

    r = client.get(f"/api/contracts/{TABLE}/review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved_count"] == 0
    email = next(c for c in body["columns"] if c["column"] == "email")
    assert email["review"]["status"] == "pending"
    assert email["definition"] == "Updated email address"


def test_settings_includes_doubled_timeouts(review_client):
    client, _store = review_client
    r = client.get("/api/settings")
    assert r.status_code == 200, r.text
    timeouts = r.json()["timeouts"]
    assert timeouts["scan_wait_timeout_sec"] == 360
    assert timeouts["sse_ping_timeout_sec"] == 30.0
