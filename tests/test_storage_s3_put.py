"""S3 storage hardening — ContentLength + corrupt YAML reads."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend, S3Backend, S3Config


def test_get_active_ignores_corrupt_yaml(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, "active-contracts")
    key = store._active_key("data.telco_cdr_event")
    backend.put_text("active-contracts", key, "database_name: data\n---\n", "application/x-yaml")

    assert store.get_active("data.telco_cdr_event") is None


def test_get_yaml_raises_on_empty(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    backend.put_text("b", "empty.yaml", "   ", "application/x-yaml")
    with pytest.raises(ValueError, match="empty or unreadable"):
        backend.get_yaml("b", "empty.yaml")


def test_s3_put_bytes_sets_content_length_and_retries_incomplete_body():
    cfg = S3Config(
        endpoint_url="http://localhost:9000",
        aws_access_key_id="k",
        aws_secret_access_key="s",
    )
    backend = S3Backend(cfg)
    client = MagicMock()
    backend.client = client
    backend._known_buckets.add("contracts")

    payload = b"hello contract"
    backend.put_bytes("contracts", "active/data.table.yaml", payload, "application/x-yaml")

    assert client.put_object.call_count == 1
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["ContentLength"] == len(payload)
    assert kwargs["Body"] == payload

    from botocore.exceptions import ClientError

    client.reset_mock()
    client.put_object.side_effect = [
        ClientError({"Error": {"Code": "IncompleteBody", "Message": "bad"}}, "PutObject"),
        {},
    ]
    backend.put_bytes("contracts", "active/data.table.yaml", payload)
    assert client.put_object.call_count == 2
    retry_kwargs = client.put_object.call_args.kwargs
    assert retry_kwargs["ContentLength"] == len(payload)
