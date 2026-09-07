"""CLI tests for `redibis approved` over a persisted session dir."""

import json
import subprocess
import sys
from pathlib import Path

from redibis.services.session_service import (
    ApprovedProperty, ApprovedSet, ScanSession,
)
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


def test_cli_approved_list_preview_merge(tmp_path):
    session_dir = tmp_path / "sess1"
    session_dir.mkdir()
    (session_dir / "data.csv").write_text("email\na@b.com\n", encoding="utf-8")
    session = ScanSession(session_id="sess1", table_name="data.sess1",
                          data_path=str(session_dir / "data.csv"))
    session.approved = ApprovedSet(items=[
        ApprovedProperty(prop_id="p1", kind="pii", column="email",
                         payload={"name": "email", "logicalType": "string",
                                  "pii": {"entity_type": "EMAIL_ADDRESS"}}),
    ])
    session.persist_to_disk()

    storage = tmp_path / "storage"
    backend = LocalBackend(storage)
    store = ContractStore(backend, bucket="contracts")

    from redibis.cli.tools import cmd_approved

    class Args:
        session = str(session_dir)
        approved_action = "list"
        prop = None
        column = None
        from_json = None
        only_merged = False
        no_validate = True
        json = True

    assert cmd_approved(Args(), store, backend) == 0

    Args.approved_action = "preview"
    assert cmd_approved(Args(), store, backend) == 0

    Args.approved_action = "merge"
    assert cmd_approved(Args(), store, backend) == 0

    approved = json.loads((session_dir / "approved.json").read_text(encoding="utf-8"))
    assert approved["summary"]["merged"] == 1

    det = {"column": "email", "detected": True, "entity_type": "EMAIL_ADDRESS", "confidence": 0.9}
    det_path = session_dir / "det.json"
    det_path.write_text(json.dumps(det), encoding="utf-8")
    Args.approved_action = "add-pii"
    Args.from_json = str(det_path)
    Args.column = "email"
    assert cmd_approved(Args(), store, backend) == 0

    # smoke: argparse entry exists
    r = subprocess.run(
        [sys.executable, "-m", "redibis.cli.main", "approved", "add-pii", "--help"],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
    )
    assert r.returncode == 0
    assert "--from-json" in r.stdout
