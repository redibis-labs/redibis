"""Optional YData Profiling helpers (``redibis[ydata]``). Lazy — never imported at package init."""

from __future__ import annotations

from typing import Any


def ydata_available() -> bool:
    """Return True when ``ydata-profiling`` is importable."""
    try:
        import ydata_profiling  # noqa: F401
        return True
    except ImportError:
        return False


def _profile(df, **opts):
    try:
        from ydata_profiling import ProfileReport
    except ImportError as e:
        raise ImportError(
            "YData Profiling not installed. Install with: pip install 'redibis[ydata]' "
            "or use the native relationships capability instead."
        ) from e
    opts.setdefault("title", "redibis EDA")
    opts.setdefault("minimal", True)
    return ProfileReport(df, **opts)


def profile_report(df, **opts):
    """Build a YData ``ProfileReport`` (lazy import)."""
    return _profile(df, **opts)


def save_html(df, path: str, **opts) -> str:
    """Render a YData HTML report to ``path`` and return the path."""
    rep = _profile(df, **opts)
    rep.to_file(path)
    return path


def save_html_summary(df, path: str, **opts) -> dict[str, Any]:
    """
    Save HTML report and return a compact summary for agent chat / run feed.

    Returns ``{html_path, row_count, column_count, alerts, type_hints}``.
    """
    rep = _profile(df, **opts)
    rep.to_file(path)
    desc = rep.get_description()
    alerts = _extract_alerts(desc)
    hints = _extract_type_hints(desc)
    variables = getattr(desc, "variables", None) or (
        desc.get("variables") if isinstance(desc, dict) else {}
    ) or {}
    table_stats = getattr(desc, "table", None) or (
        desc.get("table") if isinstance(desc, dict) else {}
    ) or {}
    row_count = table_stats.get("n") if isinstance(table_stats, dict) else getattr(table_stats, "n", None)
    return {
        "html_path": path,
        "row_count": row_count,
        "column_count": len(variables) if variables else len(getattr(df, "columns", [])),
        "alerts": alerts[:20],
        "type_hints": hints,
    }


def show_in_notebook(df, **opts):
    """Inline iframe in Jupyter."""
    return _profile(df, **opts).to_notebook_iframe()


def report_alerts(df, **opts) -> list[str]:
    """Candidate quality signal: YData warnings (constant, high-cardinality, high-corr, …)."""
    rep = _profile(df, **opts)
    return _extract_alerts(rep.get_description())


def report_type_hints(df, **opts) -> dict[str, str]:
    """Map YData inferred variable types → ``{column: type}`` (statistical, not semantic/PII)."""
    rep = _profile(df, **opts)
    return _extract_type_hints(rep.get_description())


def _extract_alerts(desc: Any) -> list[str]:
    alerts = getattr(desc, "alerts", None) or (
        desc.get("alerts") if isinstance(desc, dict) else []
    )
    return [str(a) for a in (alerts or [])]


def _extract_type_hints(desc: Any) -> dict[str, str]:
    variables = getattr(desc, "variables", None) or (
        desc.get("variables") if isinstance(desc, dict) else {}
    )
    out: dict[str, str] = {}
    for col, v in (variables or {}).items():
        if isinstance(v, dict):
            out[str(col)] = str(v.get("type", ""))
        else:
            out[str(col)] = str(getattr(v, "type", ""))
    return out
