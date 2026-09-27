"""One live contract: Contract page, Steward Review and share links edit the same object."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("USE_LOCAL_STORAGE", "true")
os.environ.setdefault("LOCAL_STORAGE_ROOT", "/tmp/redibis_live_contract_storage")
os.environ.setdefault("SCAN_OUTPUT_DIR", "/tmp/redibis_live_contract_output")
os.environ.setdefault("CONFIGS_DIR", "/tmp/redibis_live_contract_configs")

from redibis.contracts.privacy import column_is_pii  # noqa: E402
from redibis.services.session.approved import pii_row_to_fragment  # noqa: E402
from redibis.services.steward_review_service import StewardReviewService  # noqa: E402
from redibis.store.contract_store import ContractStore  # noqa: E402
from redibis.store.storage_backend import LocalBackend  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _contract(table: str) -> dict:
    db, name = table.split(".")
    return {"apiVersion": "v3.0.1", "kind": "DataContract", "name": f"{name}_contract",
            "database_name": db, "table_name": name, "version": "1.0.0",
            "schema": [{"name": name, "properties": [{"name": "id", "logicalType": "integer"},
                                                      {"name": "email", "logicalType": "string"}]}]}


def _seed(store, table: str) -> None:
    store.upsert(_contract(table), table=table, workflow="manual", run_id="r1")
    store.set_pii_decision(table, "email", "pii", entity_type="EMAIL_ADDRESS", decided_by="scan",
                           payload=pii_row_to_fragment({"column": "email", "detected": True,
                                                        "entity_type": "EMAIL_ADDRESS", "confidence": 0.9}))


def _prop(store, table: str, column: str) -> dict:
    return next(p for p in store.get_active(table)["schema"][0]["properties"] if p["name"] == column)


def _steward_save(svc, table: str, column: str, is_pii: bool, *, classification: str, tags: list):
    """What the Steward Review page sends on "save": PII, definition, tags, classification."""
    def v(field, value):
        return {"field": field, "decision": "edit", "chosen_source": "human", "value": value,
                "rationale_code": "domain_knowledge", "rationale_text": ""}
    svc.decide_many(table, column, [
        v("pii", {"is_pii": is_pii, "entity_type": "EMAIL_ADDRESS" if is_pii else None}),
        v("definition", "customer e-mail"), v("tags", tags), v("classification", classification)],
        actor="steward")


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield ContractStore(LocalBackend(tmp), "c")


def test_steward_pii_off_and_on_really_switch_the_column(store):
    """The tags / classification saved with the switch used to turn PII back on."""
    table = "db.customers"
    _seed(store, table)
    svc = StewardReviewService(store)
    before = svc.column(table, "email", actor="steward")["current"]
    _steward_save(svc, table, "email", False,
                  classification=before["classification"], tags=before["tags"])   # the form's old values
    prop = _prop(store, table, "email")
    assert not column_is_pii(prop) and "pii" not in (prop.get("tags") or [])
    assert {c["column"]: c["pii"] for c in svc.overview(table)["columns"]}["email"] is False
    assert prop["description"] == "customer e-mail"

    _steward_save(svc, table, "email", True, classification="", tags=[])
    prop = _prop(store, table, "email")
    assert column_is_pii(prop) and prop["entity_type"] == "EMAIL_ADDRESS"
    assert (prop["privacy"].get("masking_policy") or {}).get("default_strategy")      # masked again
    assert {c["column"]: c["pii"] for c in svc.overview(table)["columns"]}["email"] is True


def test_steward_classification_edit_keeps_the_masking(store):
    table = "db.customers"
    _seed(store, table)
    masking = _prop(store, table, "email")["privacy"]["masking_policy"]
    svc = StewardReviewService(store)
    _steward_save(svc, table, "email", True, classification="pii_sensitive", tags=["pii", "contact"])
    prop = _prop(store, table, "email")
    assert prop["classification"] == "pii_sensitive" and prop["privacy"]["masking_policy"] == masking
    assert prop["tags"] == ["pii", "contact"]


def test_classification_of_a_normal_column_does_not_make_it_pii(store):
    table = "db.customers"
    _seed(store, table)
    _steward_save(StewardReviewService(store), table, "id", False, classification="internal", tags=["key"])
    prop = _prop(store, table, "id")
    assert not column_is_pii(prop) and prop["tags"] == ["key"]


def test_share_edits_go_through_the_overlays(store):
    """A share-link description was written into the contract body and undone by the
    definition overlay on the same write."""
    from redibis.services.contract_edits import apply_column_edits

    table = "db.customers"
    _seed(store, table)
    store.patch_definitions(table, column_patches={"id": {"description": "from the contract page"}})
    out = apply_column_edits(store, table, {"id": {"description": "from a share link", "tags": ["key"]},
                                            "email": {"classification": "pii_sensitive"}},
                             decided_by="share:abc")
    assert sorted(out["applied"]) == ["email.classification", "id.description", "id.tags"]
    assert _prop(store, table, "id")["description"] == "from a share link"
    assert _prop(store, table, "email")["classification"] == "pii_sensitive"
    assert _prop(store, table, "email")["privacy"]["masking_policy"]           # still masked
    scoped = apply_column_edits(store, table, {"id": {"description": "no"}}, decided_by="share:x",
                                allowed=lambda f: f != "description")
    assert scoped["applied"] == [] and _prop(store, table, "id")["description"] == "from a share link"


def test_steward_decisions_as_yaml_attach_to_another_scan(store, tmp_path):
    from redibis.review.steward_attach import attach_steward_verdict_path

    table = "db.customers"
    _seed(store, table)
    svc = StewardReviewService(store)
    _steward_save(svc, table, "email", False, classification="", tags=[])
    path = tmp_path / "steward_verdicts.yaml"
    path.write_text(yaml.safe_dump(svc.export_verdicts(table, actor="steward")), encoding="utf-8")

    other = ContractStore(LocalBackend(str(tmp_path / "other")), "c")
    _seed(other, table)                                            # a new scan finds email PII again
    report = attach_steward_verdict_path(other, table, path, actor="cli")
    assert "email" in " ".join(report.applied)
    assert not column_is_pii(_prop(other, table, "email"))


# ── the Contract page ────────────────────────────────────────────────────────

def test_contract_page_switches_head_and_yaml():
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    table = f"live.t{uuid.uuid4().hex[:8]}"
    _seed(backend._cs(), table)
    client = TestClient(backend.app)
    head = client.get(f"/api/contracts/{table}/head").json()
    assert head["version"] == backend._cs().get_active(table)["version"]

    r = client.post(f"/api/contracts/{table}/columns/email/pii", json={"on": False})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/contracts/{table}/head").json()["version"] == r.json()["version_after"]
    steward = client.get(f"/api/contracts/{table}/steward").json()
    assert {c["column"]: c["pii"] for c in steward["columns"]}["email"] is False       # same object
    r = client.post(f"/api/contracts/{table}/columns/email/pii", json={"on": True, "entity_type": "EMAIL_ADDRESS"})
    assert r.status_code == 200
    steward = client.get(f"/api/contracts/{table}/steward").json()
    assert {c["column"]: c["pii"] for c in steward["columns"]}["email"] is True

    text = client.get(f"/api/contracts/{table}/yaml").text
    assert yaml.safe_load(text)["version"] == client.get(f"/api/contracts/{table}").json()["version"]
    assert "attachment" in client.get(f"/api/contracts/{table}/yaml?download=true").headers["content-disposition"]
    exported = client.get(f"/api/contracts/{table}/steward/export/verdicts?format=yaml")
    assert exported.status_code == 200 and yaml.safe_load(exported.text)["kind"]
    assert client.get("/api/contracts/live.missing/head").status_code == 404


def test_contract_tab_is_the_quick_editor():
    html = (REPO / "redibis/webapp/templates/v2.html").read_text(encoding="utf-8")
    for needle in ('["details","Details"]', "async function renderDetails", "function switchPii",
                   "function toggleRule", "function saveDefinition", "function watchContract",
                   "/head", "/yaml", "setContractFmt('json')"):
        assert needle in html, needle
    quick = html[html.index("async function renderContract"):html.index("function setContractFmt")]
    assert "/pii-decisions" in quick and "/quality-view" in quick


def test_a_scanned_pii_column_keeps_its_own_masking_when_edited(store):
    """No steward decision yet: the column's own (scan) masking is kept, not a default."""
    from redibis.services.contract_edits import apply_column_edits

    table = "db.customers"
    contract = _contract(table)
    fragment = pii_row_to_fragment({"column": "email", "detected": True,
                                    "entity_type": "EMAIL_ADDRESS", "confidence": 0.9})
    fragment.pop("_column_evidence", None)
    fragment["privacy"]["masking_policy"]["default_strategy"] = "hash"          # the scan's choice
    contract["schema"][0]["properties"][1].update(fragment)
    store.upsert(contract, table=table, workflow="manual", run_id="scan")
    assert store.get_pii_decisions(table) == {}
    apply_column_edits(store, table, {"email": {"classification": "pii_sensitive"}}, decided_by="t")
    prop = _prop(store, table, "email")
    assert prop["classification"] == "pii_sensitive"
    assert prop["privacy"]["masking_policy"]["default_strategy"] == "hash"


def test_a_plain_classification_does_not_make_a_column_pii(store):
    from redibis.services.contract_edits import apply_column_edits

    table = "db.customers"
    _seed(store, table)
    apply_column_edits(store, table, {"id": {"classification": "internal"}}, decided_by="share:x")
    prop = _prop(store, table, "id")
    assert prop["classification"] == "internal" and not column_is_pii(prop)
