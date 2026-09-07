"""CLI smoke tests for enrichment packs."""

from __future__ import annotations

from pathlib import Path

from redibis.cli.main import main

EXAMPLE_PACK = str(
    Path(__file__).resolve().parents[1] / "examples" / "enrichment_packs" / "telco"
)


def test_pack_validate_and_inspect(capsys):
    assert main(["pack", "validate", EXAMPLE_PACK]) == 0
    assert main(["pack", "inspect", EXAMPLE_PACK, "--json"]) == 0
    out = capsys.readouterr().out
    assert "telco-enrichment-lite" in out


def test_pack_evaluate_offline():
    assert main(["pack", "evaluate", EXAMPLE_PACK, "--json"]) == 0


def test_enrich_help_shows_pack():
    # argparse --help raises SystemExit
    try:
        main(["enrich", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
