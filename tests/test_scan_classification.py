"""Classification phase during unified scan when enabled."""

from __future__ import annotations

import pandas as pd

from redibis.config import RedibisConfig
from redibis.scan.base import Scan
from redibis.services.scan_service import ScanConfig


def test_scan_runs_classification_when_enabled():
    cfg = ScanConfig(
        table="telecom.customers",
        run_profile=True,
        run_quality=False,
        run_pii=True,
    )
    rb = RedibisConfig()
    rb.classification.enabled = True
    rb.classification.default_jurisdiction = "EU"

    scan = Scan(cfg, redibis_config=rb)
    df = pd.DataFrame({
        "msisdn": ["+201012345678", "+201098765432"],
        "email": ["a@b.com", "c@d.com"],
    })
    result = scan.run(df)

    assert result.status == "success"
    assert result.classification
    columns = {row["column"] for row in result.classification}
    assert "msisdn" in columns or "email" in columns
