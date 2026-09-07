"""CLI lifecycle: golden tutorial CSV → PII scan → enrich → catalog dry-run.

Validates the all-CLI tutorial path with the offline ``demo`` enrich provider
(no SGLang / OpenMetadata server required).

Run:
  pytest tests/test_cli_pii_enrich_catalog_lifecycle.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from redibis.cli.main import _build_backend, _run_enrich, _run_scan
from redibis.config import RedibisConfig
from redibis.services.catalog.openmetadata import build_openmetadata_plan
from redibis.store.contract_store import ContractStore
from tests.contract_helpers import col_pii_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "tests/data"
TUTORIAL_CSV = DATA_DIR / "golden_tutorial_customers.csv"
EXPECTED = DATA_DIR / "packs/pii_golden_tutorial_customers_v1.json"
TUTORIAL_ASSETS = DATA_DIR / "tutorial_pii_enrich"
TABLE = "golden.tutorial_customers"
TUTORIAL_CONFIG = REPO_ROOT / "config/examples/pii-csv-enrich-catalog-tutorial.yaml"

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def _require_presidio():
    pytest.importorskip("presidio_analyzer")


@pytest.fixture
def expected_verdicts() -> dict:
    assert EXPECTED.is_file(), f"missing expected verdicts: {EXPECTED}"
    return json.loads(EXPECTED.read_text(encoding="utf-8"))


def _scan_args(tmp_path: Path, *, automerge: str = "pii") -> SimpleNamespace:
    output_root = tmp_path / "scan_output"
    return SimpleNamespace(
        file=str(TUTORIAL_CSV),
        table=TABLE,
        mode="pii",
        automerge=automerge,
        equation="independent",
        pii_engines="regex",
        gliner_model="",
        ner_model="",
        no_ge_docs=True,
        no_validate=True,
        session_id=None,
        scan_output_dir=str(output_root),
        output_dir=str(output_root),
        use_s3=False,
        s3_endpoint=None,
        s3_runs_bucket="pii-reports",
        s3_contracts_bucket="active-contracts",
        s3_pii_runs_bucket="pii-contracts",
        s3_quality_runs_bucket="quality-contracts",
        config=None,
        profiler_engine=None,
        no_phonenumbers=False,
        enable_memory=False,
        enable_metadata=False,
        enable_pushdown=False,
    )


def _enrich_args(tmp_path: Path) -> SimpleNamespace:
    output_root = tmp_path / "scan_output"
    return SimpleNamespace(
        table=TABLE,
        provider="demo",
        providers_file=None,
        list_providers=False,
        model=None,
        endpoint=None,
        api_key=None,
        contract_file=None,
        context=[str(TUTORIAL_ASSETS / "context/domain_glossary.md")],
        example_docs=[],
        examples=None,
        prompt=str(TUTORIAL_ASSETS / "prompts/system_prompt.md"),
        instructions=str(TUTORIAL_ASSETS / "instructions.md"),
        pack=None,
        dry_run=False,
        approve_context_reduction=None,
        external_masked_ack=False,
        bypass_rai=False,
        automerge=False,
        config=str(TUTORIAL_CONFIG) if TUTORIAL_CONFIG.is_file() else None,
        output_dir=str(output_root),
        use_s3=False,
        s3_endpoint=None,
        s3_runs_bucket="pii-reports",
        s3_contracts_bucket="active-contracts",
        s3_pii_runs_bucket="pii-contracts",
        s3_quality_runs_bucket="quality-contracts",
    )





def test_tutorial_csv_pii_regex_matches_expected(tmp_path, expected_verdicts):
    """Golden tutorial CSV produces the expected regex PII verdicts."""
    assert TUTORIAL_CSV.is_file(), f"missing fixture: {TUTORIAL_CSV}"
    args = _scan_args(tmp_path, automerge="pii")
    backend = _build_backend(args)
    store = ContractStore(backend, bucket=args.s3_contracts_bucket)
    assert _run_scan(args, store, backend) == 0

    active = store.get_active(TABLE)
    assert active is not None
    props = {p["name"]: p for p in active["schema"][0]["properties"]}

    for col in expected_verdicts["required_pii_columns"]:
        ce = col_pii_engine(props[col])
        exp = expected_verdicts["columns"][col]
        assert ce.get("detected") is True, col
        assert ce.get("entity_type") == exp["entity_type"], col

    for col in expected_verdicts["required_non_pii_columns"]:
        ce = col_pii_engine(props[col])
        assert not ce.get("detected"), col


def test_cli_scan_enrich_catalog_dry_run_lifecycle(tmp_path, expected_verdicts):
    """scan --automerge pii → enrich (demo + custom prompt/context) → catalog dry-run."""
    from redibis.services.catalog_service import CatalogService

    assert TUTORIAL_CSV.is_file()
    assert (TUTORIAL_ASSETS / "prompts/system_prompt.md").is_file()
    assert (TUTORIAL_ASSETS / "context/domain_glossary.md").is_file()

    scan_args = _scan_args(tmp_path, automerge="pii")
    backend = _build_backend(scan_args)
    store = ContractStore(backend, bucket=scan_args.s3_contracts_bucket)
    assert _run_scan(scan_args, store, backend) == 0
    assert store.get_active(TABLE) is not None

    enrich_args = _enrich_args(tmp_path)
    assert _run_enrich(enrich_args, store) == 0
    active = store.get_active(TABLE)
    assert active is not None
    props = {p["name"]: p for p in active["schema"][0]["properties"]}

    for col in expected_verdicts["required_pii_columns"]:
        prop = props[col]
        assert prop.get("businessName"), f"{col} missing businessName after enrich"
        business = prop.get("business") or {}
        assert business.get("definition"), f"{col} missing business.definition after enrich"

    # Explicit top-level description still absent for demo enrich; OM mapping
    # must fall back to business.definition.
    email_prop = props["email"]
    assert not (email_prop.get("description") or "").strip()
    assert (email_prop.get("business") or {}).get("definition")

    plan = build_openmetadata_plan(active, TABLE)
    om_cols = {c["name"]: c for c in plan.table_payload["columns"]}
    assert "Demo business definition" in om_cols["email"]["description"]
    assert om_cols["email"]["displayName"]
    email_tags = {t["tagFQN"] for t in om_cols["email"]["tags"]}
    assert "PII.NonSensitive" in email_tags or "PII.Sensitive" in email_tags
    assert "Redibis.EMAIL_ADDRESS" in email_tags
    nid_tags = {t["tagFQN"] for t in om_cols["national_id"]["tags"]}
    assert "PII.Sensitive" in nid_tags, nid_tags
    assert "Redibis.EG_NATIONAL_ID" in nid_tags

    cfg = RedibisConfig.from_yaml(TUTORIAL_CONFIG)
    svc = CatalogService.from_redibis_config(store, cfg)
    result = svc.push(TABLE, dry_run=True, backend="openmetadata")
    assert result.dry_run is True
    assert result.backend == "openmetadata"
    assert result.table == TABLE
    assert "tutorial_customers" in (result.entity_fqn or "")
    assert result.preview is not None
    assert result.preview.get("backend") == "openmetadata"


def test_cli_help_rejects_removed_scan_flags():
    """Tutorial must not document removed ``--use-local`` / ``--auto-write`` flags."""
    from redibis.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main(["scan", "--help"])
    # argparse --help exits 0; capture via subprocess-style is awkward — instead
    # inspect the parser help text through a dedicated invocation helper.
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            main(["scan", "--help"])
        except SystemExit as e:
            assert e.code in (0, None)
    help_text = buf.getvalue()
    assert "--automerge" in help_text
    assert "--use-local" not in help_text
    assert "--auto-write" not in help_text


def test_runs_merge_help_requires_kind():
    import io
    from contextlib import redirect_stdout

    from redibis.cli.main import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            main(["runs", "merge", "--help"])
        except SystemExit as e:
            assert e.code in (0, None)
    help_text = buf.getvalue()
    assert "--kind" in help_text


def test_tutorial_config_enables_classification_and_catalog():
    assert TUTORIAL_CONFIG.is_file()
    cfg = RedibisConfig.from_yaml(TUTORIAL_CONFIG)
    assert cfg.classification.enabled is True
    assert cfg.classification.policy_pack == "telecom"
    assert cfg.catalog.backend == "openmetadata"
    assert cfg.catalog.push.tags is True
    assert cfg.catalog.push.glossary is True
    assert cfg.catalog.push.contract is True
    assert cfg.pii.engines == "regex"
    assert cfg.contract.automerge in ("pii", "both")
