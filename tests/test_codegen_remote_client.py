"""Tests for RemoteCodegenClient and error handling when remote mode lacks credentials."""

import pytest
from redibis.agents.codegen import submit_codegen
from redibis.agents.codegen_remote_client import RemoteCodegenClient, RemoteCodegenClientError
from redibis.config import ConfigError


def test_submit_codegen_remote_mode_missing_credentials(monkeypatch):
    monkeypatch.setenv("REDIBIS_CODEGEN_MODE", "remote")
    monkeypatch.delenv("REDIBIS_CODEGEN_SERVICE_URL", raising=False)
    monkeypatch.delenv("REDIBIS_CODEGEN_TOKEN", raising=False)

    contract = {"columns": {"col1": {"pii": "EMAIL"}}}
    with pytest.raises(ConfigError, match="missing required configuration"):
        submit_codegen(
            intent="Mask col1",
            table="public.test",
            contract=contract,
        )


def test_remote_client_missing_url_or_token():
    client = RemoteCodegenClient(service_url="", token="")
    with pytest.raises(RemoteCodegenClientError, match="Remote codegen requires both"):
        client.submit({"test": "payload"})
