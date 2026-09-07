"""
CLI integration tests — PII scan against golden e-shop fixtures.

Uses sampled rows from the 1000-record golden CSVs for reasonable CI time.
Exercises the ``redibis scan`` code path via ``_run_scan`` (same as CLI).

Run:
  pytest tests/test_cli_pii_scan.py -v
  pytest tests/test_cli_pii_scan.py -v -k regex          # fast (Presidio only)
  pytest tests/test_cli_pii_scan.py -v -m slow           # GLiNER / both engines
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from redibis.cli.main import _build_backend, _run_scan
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.subcontract_store import SubcontractStore
from tests.contract_helpers import col_pii_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
# Full realistic EG fixtures (1000 rows); sample below for CI time.
# Do not use tests/fixtures/golden stubs — those are short SA-shaped rows
# that miss recipient_mobile / EG MSISDN patterns these tests assert on.
DATA_DIR = REPO_ROOT / "tests/data"
MERCHANT_CSV = DATA_DIR / "realistic_merchant_seller_registry.csv"
ORDER_CSV = DATA_DIR / "realistic_eshop_order_header.csv"

# Full golden files are 1000 rows; sample for CI (increase for deeper runs).
SAMPLE_ROWS = 20

EXPECTED_PII_ARTIFACTS = (
    "pii_contract.yaml",
    "pii_detections.json",
    "pii_detections.html",
    "pii_regex_review.html",
    "pii_report.json",
)

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def _require_presidio():
    pytest.importorskip("presidio_analyzer")


@pytest.fixture
def merchant_sample_csv(tmp_path) -> Path:
    assert MERCHANT_CSV.is_file(), f"missing fixture: {MERCHANT_CSV}"
    out = tmp_path / "merchant_sample.csv"
    pd.read_csv(MERCHANT_CSV, nrows=SAMPLE_ROWS).to_csv(out, index=False)
    return out


@pytest.fixture
def order_sample_csv(tmp_path) -> Path:
    assert ORDER_CSV.is_file(), f"missing fixture: {ORDER_CSV}"
    out = tmp_path / "order_sample.csv"
    pd.read_csv(ORDER_CSV, nrows=SAMPLE_ROWS).to_csv(out, index=False)
    return out


def _build_scan_args(
    tmp_path: Path,
    csv_path: Path,
    table: str,
    *,
    mode: str = "pii",
    pii_engines: str = "both",
    automerge: str = "none",
) -> SimpleNamespace:
    output_root = tmp_path / "scan_output"
    return SimpleNamespace(
        file=str(csv_path),
        table=table,
        mode=mode,
        automerge=automerge,
        equation="independent",
        pii_engines=pii_engines,
        gliner_model=os.environ.get("REDIBIS_NER_MODEL", ""),
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
    )


def _run_cli_scan(
    tmp_path: Path,
    csv_path: Path,
    table: str,
    **kwargs,
) -> dict:
    """Run ``redibis scan`` (CLI handler) and return parsed session/run state."""
    args = _build_scan_args(tmp_path, csv_path, table, **kwargs)
    backend = _build_backend(args)
    store = ContractStore(backend, bucket=args.s3_contracts_bucket)
    sub_store = SubcontractStore(
        backend,
        pii_bucket=args.s3_pii_runs_bucket,
        quality_bucket=args.s3_quality_runs_bucket,
    )

    rc = _run_scan(args, store, backend)
    assert rc == 0, "CLI scan should exit 0"

    output_root = Path(args.scan_output_dir)
    session_dirs = [d for d in output_root.iterdir() if (d / "session.json").is_file()]
    assert len(session_dirs) == 1, f"expected one session dir under {output_root}"

    session_dir = session_dirs[0]
    session_meta = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    run_id = session_meta["latest_run_id"]
    assert run_id, "session should record latest_run_id"

    run_dir = session_dir / "runs" / run_id
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    artifacts_dir = run_dir / "artifacts"

    return {
        "args": args,
        "backend": backend,
        "store": store,
        "sub_store": sub_store,
        "table": table,
        "run_id": run_id,
        "manifest": manifest,
        "artifacts_dir": artifacts_dir,
        "session_dir": session_dir,
    }


def _assert_pii_artifacts_flushed(artifacts_dir: Path) -> None:
    assert artifacts_dir.is_dir(), f"missing artifacts dir: {artifacts_dir}"
    names = {p.name for p in artifacts_dir.iterdir() if p.is_file()}
    for key in EXPECTED_PII_ARTIFACTS:
        assert key in names, f"expected artifact {key!r} in {sorted(names)}"


def _assert_pii_subcontract(sub_store, table: str, run_id: str) -> dict:
    sub = sub_store.get("pii", table, run_id)
    assert sub is not None, "PII subcontract should be written to pii-contracts bucket"
    assert sub.payload.get("schema"), "subcontract payload should include schema"
    return sub.payload


# ── PII-only, engine variants ─────────────────────────────────────────────────


def _local_ner_model_path() -> str | None:
    path = os.environ.get("REDIBIS_NER_MODEL", "").strip()
    return path if path and Path(path).exists() else None


@pytest.fixture
def require_local_ner():
    if _local_ner_model_path() is None:
        pytest.skip("Set REDIBIS_NER_MODEL to an existing local GLiNER weights directory")


@pytest.mark.parametrize(
    "fixture_name,table",
    [
        ("merchant_sample_csv", "golden.merchant_seller_registry"),
        ("order_sample_csv", "golden.eshop_order_header"),
    ],
)
def test_cli_pii_only_regex(tmp_path, fixture_name, table, request):
    """``scan --mode pii --pii-engines regex`` on golden fixtures."""
    csv_path = request.getfixturevalue(fixture_name)
    state = _run_cli_scan(
        tmp_path, csv_path, table, mode="pii", pii_engines="regex",
    )
    assert state["manifest"]["status"] == "success"
    counts = state["manifest"]["counts"]
    assert counts["pii_columns_scanned"] > 0
    assert counts["pii_columns_detected"] >= 1
    _assert_pii_artifacts_flushed(state["artifacts_dir"])
    _assert_pii_subcontract(state["sub_store"], table, state["run_id"])
    assert state["store"].get_active(table) is None


@pytest.mark.parametrize(
    "fixture_name,table",
    [
        ("merchant_sample_csv", "golden.merchant_seller_registry"),
        ("order_sample_csv", "golden.eshop_order_header"),
    ],
)
@pytest.mark.slow
def test_cli_pii_only_gliner(tmp_path, fixture_name, table, request, require_local_ner):
    """``scan --mode pii --pii-engines gliner`` (NER only)."""
    pytest.importorskip("gliner")
    csv_path = request.getfixturevalue(fixture_name)
    state = _run_cli_scan(
        tmp_path, csv_path, table, mode="pii", pii_engines="gliner",
    )
    assert state["manifest"]["status"] == "success"
    assert state["manifest"]["counts"]["pii_columns_scanned"] > 0
    _assert_pii_artifacts_flushed(state["artifacts_dir"])


@pytest.mark.parametrize(
    "fixture_name,table",
    [
        ("merchant_sample_csv", "golden.merchant_seller_registry"),
    ],
)
@pytest.mark.slow
def test_cli_pii_both_engines(tmp_path, fixture_name, table, request, require_local_ner):
    """``scan --mode pii --pii-engines both`` (regex + GLiNER)."""
    pytest.importorskip("gliner")
    csv_path = request.getfixturevalue(fixture_name)
    state = _run_cli_scan(
        tmp_path, csv_path, table, mode="pii", pii_engines="both",
    )
    assert state["manifest"]["status"] == "success"
    assert state["manifest"]["counts"]["pii_columns_detected"] >= 1
    _assert_pii_artifacts_flushed(state["artifacts_dir"])


# ── Full scan (PII + quality) ───────────────────────────────────────────────


def test_cli_scan_all_mode_runs_pii_and_quality(tmp_path, merchant_sample_csv):
    """``scan --mode all`` — profile/quality + PII on golden merchant data."""
    pytest.importorskip("great_expectations")
    state = _run_cli_scan(
        tmp_path,
        merchant_sample_csv,
        "golden.merchant_seller_registry",
        mode="all",
        pii_engines="regex",
    )
    assert state["manifest"]["status"] == "success"
    counts = state["manifest"]["counts"]
    assert counts["pii_columns_detected"] >= 1
    assert counts["quality_expectations"] > 0

    artifacts = {p.name for p in state["artifacts_dir"].iterdir() if p.is_file()}
    assert "quality_contract.yaml" in artifacts
    assert "interactive_review.html" in artifacts or "triage_report.html" in artifacts
    assert state["sub_store"].get(
        "quality", "golden.merchant_seller_registry", state["run_id"],
    ) is not None


# ── Contract creation ─────────────────────────────────────────────────────────


def test_cli_pii_subcontract_without_merge(tmp_path, merchant_sample_csv):
    """PII subcontract written; active contract unchanged (no automerge)."""
    state = _run_cli_scan(
        tmp_path,
        merchant_sample_csv,
        "golden.merchant_seller_registry",
        mode="pii",
        pii_engines="regex",
        automerge="none",
    )
    payload = _assert_pii_subcontract(
        state["sub_store"], "golden.merchant_seller_registry", state["run_id"],
    )
    assert payload.get("apiVersion")
    assert state["store"].get_active("golden.merchant_seller_registry") is None
    assert state["backend"].exists(
        "pii-contracts",
        f"golden.merchant_seller_registry/{state['run_id']}.yaml",
    )


def test_cli_pii_automerge_merges_active_contract(tmp_path, merchant_sample_csv):
    """``scan --automerge pii`` merges PII run into active contract."""
    state = _run_cli_scan(
        tmp_path,
        merchant_sample_csv,
        "golden.merchant_seller_registry",
        mode="pii",
        pii_engines="regex",
        automerge="pii",
    )
    assert state["manifest"].get("pii_contract_version")
    active = state["store"].get_active("golden.merchant_seller_registry")
    assert active is not None

    props = {p["name"]: p for p in active["schema"][0]["properties"]}
    assert col_pii_engine(props["merchant_email"]).get("detected") is True
    assert col_pii_engine(props["merchant_mobile"]).get("detected") is True

    sub = state["sub_store"].get(
        "pii", "golden.merchant_seller_registry", state["run_id"],
    )
    assert sub.status == "merged"


# ── Reports flush ─────────────────────────────────────────────────────────────


def test_cli_pii_report_artifacts_content(tmp_path, order_sample_csv):
    """Flushed artifacts are valid YAML/JSON with expected PII columns."""
    state = _run_cli_scan(
        tmp_path,
        order_sample_csv,
        "golden.eshop_order_header",
        mode="pii",
        pii_engines="regex",
    )
    ad = state["artifacts_dir"]
    _assert_pii_artifacts_flushed(ad)

    contract = yaml.safe_load((ad / "pii_contract.yaml").read_text(encoding="utf-8"))
    col_names = {p["name"] for p in contract["schema"][0]["properties"]}
    assert "recipient_mobile" in col_names
    assert "shipping_address_text" in col_names

    detections = json.loads((ad / "pii_detections.json").read_text(encoding="utf-8"))
    assert isinstance(detections, list)
    assert len(detections) >= 1
    detected_cols = {d["column"] for d in detections if d.get("detected")}
    assert detected_cols, "expected at least one detected column in JSON report"

    html = (ad / "pii_detections.html").read_text(encoding="utf-8")
    assert "PII Detection Report" in html

    manifest = json.loads(
        (state["session_dir"] / "runs" / state["run_id"] / "run_manifest.json").read_text(
            encoding="utf-8",
        )
    )
    rel = manifest["artifacts"]
    assert rel.get("pii_contract") == "pii_contract.yaml"
    assert rel.get("pii_detections") == "pii_detections.json"
