"""Phase 1–2: lazy store accessors and readiness probe."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "scan_output"))
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "configs"))


def test_backend_imports_when_storage_unreachable(monkeypatch, local_env):
    import redibis.webapp.store_accessors as accessors

    def _boom():
        raise ConnectionError("storage down")

    monkeypatch.setattr(accessors, "_build_storage", _boom)
    accessors.clear_stores()

    import redibis.webapp.backend as backend

    importlib.reload(backend)
    from fastapi.testclient import TestClient

    client = TestClient(backend.app)
    assert client.get("/health").status_code == 200
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["checks"]["storage"] is False


def test_dead_handle_rebuilds(monkeypatch, local_env, tmp_path):
    from redibis.store.storage_backend import LocalBackend
    import redibis.webapp.store_accessors as accessors

    first = LocalBackend(str(tmp_path / "b1"))
    second = LocalBackend(str(tmp_path / "b2"))
    calls = {"n": 0}

    def _factory():
        calls["n"] += 1
        return first if calls["n"] == 1 else second

    monkeypatch.setattr(accessors, "_build_storage", _factory)
    accessors.clear_stores()

    got1 = accessors.get_backend_store()
    assert got1 is first

    monkeypatch.setattr(first, "ping", lambda: False)
    accessors._last_ping = 0.0
    got2 = accessors.get_backend_store()
    assert got2 is second
    assert calls["n"] == 2


def test_ready_all_pass(monkeypatch, local_env):
    from fastapi.testclient import TestClient
    import redibis.webapp.backend as backend

    importlib.reload(backend)
    client = TestClient(backend.app)
    res = client.get("/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["ready"] is True
    assert all(body["checks"].values())


def test_ping_ttl_limits_repeated_pings(monkeypatch, local_env, tmp_path):
    from redibis.store.storage_backend import LocalBackend
    import redibis.webapp.store_accessors as accessors

    backend = LocalBackend(str(tmp_path / "b"))
    ping_count = {"n": 0}
    real_ping = backend.ping

    def counting_ping():
        ping_count["n"] += 1
        return real_ping()

    backend.ping = counting_ping  # type: ignore[method-assign]
    monkeypatch.setattr(accessors, "_build_storage", lambda: backend)
    accessors.clear_stores()

    for _ in range(5):
        accessors.get_backend_store()
    assert ping_count["n"] <= 1


def test_with_store_retry_rebuilds_on_connection_error(monkeypatch, local_env):
    import redibis.webapp.store_accessors as accessors

    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("dead handle")
        return "ok"

    accessors.clear_stores()
    assert accessors.with_store_retry(flaky) == "ok"
    assert attempts["n"] == 2


def test_ready_storage_timeout(monkeypatch, local_env):
    import time
    from fastapi.testclient import TestClient
    import redibis.webapp.backend as backend

    monkeypatch.setenv("REDIBIS_READY_TIMEOUT_SEC", "0.2")
    monkeypatch.setattr(backend, "_READY_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(backend, "load_global_settings", lambda: {})
    monkeypatch.setattr(backend, "memory_ready", lambda: True)

    def slow_ping():
        time.sleep(1.0)
        return True

    monkeypatch.setattr(backend, "backend_ping", slow_ping)
    client = TestClient(backend.app)
    t0 = time.perf_counter()
    res = client.get("/ready")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.8
    assert res.status_code == 503
    assert res.json()["checks"]["storage"] is False
