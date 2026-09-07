"""Unit tests for contract lifecycle diff engine."""

import pytest

from redibis.contracts.contract_diff import diff_contracts, render_diff_md


def test_diff_contracts_override_add_unchanged():
    c_det = {
        "schema": [{
            "name": "t",
            "properties": [
                {
                    "name": "email",
                    "classification": "pii_personal",
                    "tags": ["pii"],
                    "privacy": {"classification_engine": {"entity_type": "EMAIL"}},
                },
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    c_llm = {
        "schema": [{
            "name": "t",
            "properties": [
                {
                    "name": "email",
                    "classification": "pii_personal",
                    "tags": ["pii", "contact"],
                    "privacy": {"classification_engine": {"entity_type": "EMAIL_ADDRESS"}},
                    "business": {"definition": "Customer email"},
                },
                {
                    "name": "city",
                    "business": {"definition": "City name"},
                },
            ],
        }],
    }
    diff = diff_contracts(c_det, c_llm)
    assert diff["summary"]["adds"] >= 2
    assert diff["summary"]["overrides"] >= 1

    email_fields = [f for f in diff["fields"] if f["column"] == "email"]
    kinds = {f["field"]: f["kind"] for f in email_fields}
    assert kinds.get("business.definition") == "add"
    assert kinds.get("entity_type") == "override"
    assert kinds.get("classification") == "unchanged"


def test_render_diff_md_groups_columns():
    diff = {
        "summary": {"overrides": 1, "adds": 1, "unchanged": 4},
        "fields": [
            {
                "column": "email",
                "field": "business.definition",
                "kind": "add",
                "det_value": None,
                "llm_value": "Customer email",
            },
            {
                "column": "email",
                "field": "entity_type",
                "kind": "override",
                "det_value": "EMAIL",
                "llm_value": "EMAIL_ADDRESS",
            },
        ],
    }
    md = render_diff_md(diff)
    assert "## LLM changed" in md
    assert "## LLM added" in md
    assert "email" in md
    assert "business.definition" in md
