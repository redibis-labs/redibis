"""Run registry helpers — digest key creation must not require a prior scan."""

from __future__ import annotations

from redibis.pii.run_store import input_digest


def test_input_digest_on_a_fresh_configs_dir_creates_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    monkeypatch.delenv("REDIBIS_PII_RUN_HMAC_KEY", raising=False)
    monkeypatch.delenv("REDIBIS_PII_RUN_DIR", raising=False)
    assert not (tmp_path / "pii_runs").exists()
    d = input_digest("some text long enough to digest!!")
    assert d.startswith("hmac-sha256:")
    assert (tmp_path / "pii_runs" / ".hmac_key").is_file()
