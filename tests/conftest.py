import os

import pytest
from redibis.store.storage_backend import LocalBackend
from redibis.store.contract_store import ContractStore
from redibis.pii.contract_writer import PIIContractWriter
from redibis.models import PIIDetection

# Faster password hashing in the test process; production default remains 200_000.
os.environ.setdefault("REDIBIS_PBKDF2_ROUNDS", "2000")


@pytest.fixture(autouse=True)
def _clear_deployment_model_env(monkeypatch):
    """Avoid host shell REDIBIS_* leaking into tests (e.g. export-airgap /models)."""
    monkeypatch.delenv("REDIBIS_MODELS_DIR", raising=False)
    monkeypatch.delenv("REDIBIS_NER_MODEL", raising=False)


@pytest.fixture(autouse=True)
def _dashboard_auth_off_by_default(monkeypatch, request):
    """Keep existing TestClient suites on the pre-auth contract.

    ``tests/test_webapp_auth.py`` and ``tests/test_text_gateway_api.py``
    enable auth themselves.
    """
    basename = getattr(request.node, "fspath", None) and request.fspath.basename
    if basename in {"test_webapp_auth.py", "test_text_gateway_api.py"}:
        return
    monkeypatch.setenv("REDIBIS_AUTH_ENABLED", "0")


@pytest.fixture
def tmp_storage(tmp_path):
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def store(tmp_storage):
    return ContractStore(tmp_storage, bucket="test-contracts")


@pytest.fixture
def sample_pii_detections():
    return [
        PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER",
                     confidence=0.92, presidio_score=0.88,
                     presidio_pattern="msisdn_egypt_any_format", gliner_score=0.91),
        PIIDetection(column="national_id", detected=True, entity_type="EG_NATIONAL_ID",
                     confidence=0.97, presidio_score=0.92),
        PIIDetection(column="notes", detected=True, entity_type="PERSON",
                     confidence=0.81, gliner_score=0.81,
                     arabic_aware=True, arabic_fraction=0.74),
        PIIDetection(column="city", detected=False, decision_path="skipped_by_triage"),
    ]
