"""Tests for lightweight ``import redibis`` and lazy public API."""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_isolated(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


def test_import_redibis_does_not_load_great_expectations():
    """Bare ``import redibis`` must not pull GE into sys.modules."""
    proc = _run_isolated(
        "import sys\n"
        "import redibis\n"
        "assert redibis.__version__\n"
        "assert 'great_expectations' not in sys.modules\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_import_obs_does_not_load_services_or_ge():
    """``import redibis.obs`` / ``setup_logging`` must stay light."""
    proc = _run_isolated(
        "import sys\n"
        "\n"
        "def _loaded(prefix):\n"
        "    return any(n == prefix or n.startswith(prefix + '.') for n in sys.modules)\n"
        "\n"
        "import redibis.obs\n"
        "from redibis.obs import setup_logging\n"
        "assert callable(setup_logging)\n"
        "for name in (\n"
        "    'great_expectations',\n"
        "    'scipy',\n"
        "    'redibis.services',\n"
        "    'redibis.obs.run_log_sink',\n"
        "    'redibis.services.pipeline',\n"
        "):\n"
        "    assert not _loaded(name), name\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_import_services_package_is_lazy():
    """Plain ``import redibis.services`` must not load scan / GE."""
    proc = _run_isolated(
        "import sys\n"
        "\n"
        "def _loaded(prefix):\n"
        "    return any(n == prefix or n.startswith(prefix + '.') for n in sys.modules)\n"
        "\n"
        "import redibis.services\n"
        "assert 'ScanService' in redibis.services.__all__\n"
        "assert 'ScanService' in dir(redibis.services)\n"
        "try:\n"
        "    getattr(redibis.services, 'NotARealExport')\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('expected AttributeError')\n"
        "for name in (\n"
        "    'great_expectations',\n"
        "    'scipy',\n"
        "    'redibis.services.scan_service',\n"
        "    'redibis.services.pipeline',\n"
        "    'redibis.services.browse_service',\n"
        "    'redibis.scan',\n"
        "    'redibis.quality.gatekeeper',\n"
        "):\n"
        "    assert not _loaded(name), name\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_services_facade_resolves_scan_service():
    """Public facade still resolves (may load heavy deps once accessed)."""
    from redibis.services import ScanService

    assert ScanService.__name__ == "ScanService"


def test_services_pipeline_submodule_still_importable():
    """``from redibis.services import pipeline`` must still resolve the submodule."""
    from redibis.services import pipeline

    assert pipeline.__name__ == "redibis.services.pipeline"


def test_obs_persist_run_log_lazy_export():
    proc = _run_isolated(
        "from redibis.obs import persist_run_log\n"
        "assert callable(persist_run_log)\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_facades_lazy_and_in_dir():
    import redibis

    assert "PIIScan" in dir(redibis)
    assert "PIIScan" in redibis.__all__
    cls = redibis.PIIScan
    assert cls.__name__ == "PIIScan"


def test_scan_config_lazy_export():
    import redibis

    cfg_cls = redibis.ScanConfig
    assert cfg_cls.__name__ == "ScanConfig"


@pytest.mark.parametrize("alias,canonical", [
    ("PIISampler", "TableSampler"),
    ("PandasSampler", "PandasTableSampler"),
    ("SamplerConfig", "SamplingConfig"),
    ("PIIColumnProfile", "ColumnProfile"),
    ("ContractStoreService", "ContractStore"),
])
def test_deprecated_aliases_warn(alias, canonical):
    import redibis

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obj = getattr(redibis, alias)
    assert any(isinstance(w.message, DeprecationWarning) for w in caught)
    assert obj.__name__ == canonical
