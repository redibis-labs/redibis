"""OpenMetadata quality publisher unit tests."""

from __future__ import annotations

from datetime import datetime, timezone

from redibis.services.catalog.openmetadata_quality import (
    iter_contract_quality_rules,
    map_test_status,
    publish_quality,
    quality_case_name,
    quality_stable_rule_id,
)


def test_idempotent_test_case_names():
    sid1 = quality_stable_rule_id(
        "expect_column_values_to_not_be_null",
        "msisdn",
        {"column": "msisdn"},
    )
    sid2 = quality_stable_rule_id(
        "expect_column_values_to_not_be_null",
        "msisdn",
        {"column": "msisdn"},
    )
    assert sid1 == sid2
    assert quality_case_name(sid1) == f"redibis_{sid1}"
    # Different kwargs → different id
    sid3 = quality_stable_rule_id(
        "expect_column_values_to_not_be_null",
        "msisdn",
        {"column": "msisdn", "mostly": 0.95},
    )
    assert sid3 != sid1


def test_status_mapping():
    assert map_test_status("pass") == "Success"
    assert map_test_status("passed") == "Success"
    assert map_test_status("fail") == "Failed"
    assert map_test_status("failed") == "Failed"
    assert map_test_status("error") == "Aborted"
    assert map_test_status("skipped") == "Aborted"


def test_iter_contract_quality_rules_stable():
    contract = {
        "physicalName": "telecom.customers",
        "schema": [{
            "name": "customers",
            "physicalName": "telecom.customers",
            "properties": [{
                "name": "msisdn",
                "quality": [{
                    "engine": "greatExpectations",
                    "implementation": {
                        "expectation_type": "expect_column_values_to_not_be_null",
                        "kwargs": {"column": "msisdn"},
                    },
                }],
            }],
        }],
    }
    rules_a = iter_contract_quality_rules(contract, "telecom.customers")
    rules_b = iter_contract_quality_rules(contract, "telecom.customers")
    assert len(rules_a) == 1
    assert rules_a[0]["name"] == rules_b[0]["name"]
    assert rules_a[0]["name"].startswith("redibis_")


def test_publish_quality_idempotent_and_degrade():
    calls: list[tuple[str, dict]] = []

    class Client:
        def get_system_version(self):
            return {"version": "1.12.4"}

        def put(self, path, body):
            calls.append((path, body))
            if "testSuites" in path:
                return {"fullyQualifiedName": "redibis_telecom_customers"}
            if path.endswith("/testCaseResult"):
                return {}
            if "testCases" in path:
                return {
                    "fullyQualifiedName": (
                        f"redibis_telecom_customers.{body['name']}"
                    )
                }
            return {}

    contract = {
        "physicalName": "telecom.customers",
        "schema": [{
            "name": "customers",
            "physicalName": "telecom.customers",
            "properties": [{
                "name": "msisdn",
                "quality": [{
                    "engine": "greatExpectations",
                    "implementation": {
                        "expectation_type": "expect_column_values_to_not_be_null",
                        "kwargs": {"column": "msisdn"},
                    },
                }],
            }],
        }],
    }
    started = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    client = Client()
    for _ in range(3):
        tel = publish_quality(
            client,
            contract,
            "telecom.customers",
            table_fqn="hive.telecom.default.customers",
            results=[{"stable_id": iter_contract_quality_rules(
                contract, "telecom.customers"
            )[0]["stable_id"], "status": "pass"}],
            run_started_at=started,
        )
    assert tel["test_cases"] == 1
    # Same case name every time
    case_puts = [b for p, b in calls if p == "/v1/dataQuality/testCases"]
    assert len(case_puts) == 3
    assert len({b["name"] for b in case_puts}) == 1
    # Same timestamp on results (not now())
    result_puts = [b for p, b in calls if p.endswith("/testCaseResult")]
    assert all(b["timestamp"] == int(started.timestamp() * 1000) for b in result_puts)
    assert all(b["testCaseStatus"] == "Success" for b in result_puts)


def test_publish_quality_degrades_on_404():
    class Client:
        def get_system_version(self):
            return {"version": "1.8.0"}

        def put(self, path, body):
            raise RuntimeError("OpenMetadata PUT /v1/dataQuality/testSuites (404): not found")

    contract = {
        "physicalName": "telecom.customers",
        "schema": [{
            "name": "customers",
            "physicalName": "telecom.customers",
            "properties": [{
                "name": "msisdn",
                "quality": [{
                    "rule": "missingCount",
                    "mustBe": 0,
                }],
            }],
        }],
    }
    tel = publish_quality(
        Client(),
        contract,
        "telecom.customers",
        table_fqn="hive.telecom.default.customers",
    )
    assert tel["degraded"] is True
    assert "testSuites_unavailable" in tel["degrade_reason"]
