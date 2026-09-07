"""Parse scan mode strings (comma list or ``all``)."""

from __future__ import annotations

_VALID_SCAN_PARTS = frozenset({"profile", "pii", "quality"})


def parse_scan_mode(mode: str) -> tuple[bool, bool, bool]:
    """
    Return ``(run_pii, run_profile, run_quality)`` from a mode string.

    ``all`` runs profile + quality + PII. ``quality`` implies profiling.
    Comma lists are supported: ``pii,quality``, ``profile``, etc.
    """
    m = (mode or "all").strip().lower()
    if m in ("all", "both"):
        return True, True, True
    if m == "quality":
        return False, True, True
    if m == "pii":
        return True, False, False
    if m == "profile":
        return False, True, False
    parts = {p.strip().lower() for p in m.split(",") if p.strip()}
    unknown = parts - _VALID_SCAN_PARTS
    if unknown:
        raise ValueError(
            f"unknown scan mode part(s) {sorted(unknown)}; "
            f"choices: all, profile, pii, quality, or comma-separated list"
        )
    run_pii = "pii" in parts
    run_quality = "quality" in parts
    run_profile = "profile" in parts or run_quality
    return run_pii, run_profile, run_quality
