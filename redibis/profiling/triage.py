"""
Engine-neutral column triage and Arabic detection (pandas only).

Shared by all profiler backends — no Great Expectations, Presidio, or GLiNER imports.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import pandas as pd

from redibis.models import ColumnProfile, ARABIC_UNICODE_RE, PII_NAME_HINTS
from redibis.profiling.column_types import infer_column_types_from_series

log = logging.getLogger(__name__)

_ARABIC_PATTERN = re.compile(ARABIC_UNICODE_RE)
_PII_HINTS_LOWER = [h.lower() for h in PII_NAME_HINTS]


def string_columns(df: pd.DataFrame, columns: Optional[list[str]] = None) -> list[str]:
    """Return string-like column names from ``df`` (optionally filtered)."""
    if df is None or df.empty:
        return []
    candidates = columns or list(df.columns)
    out: list[str] = []
    for col in candidates:
        if col not in df.columns:
            continue
        series = df[col]
        if (
            pd.api.types.is_string_dtype(series)
            or pd.api.types.is_object_dtype(series)
            or str(series.dtype) in ("string", "str", "object", "StringDtype")
        ):
            out.append(col)
    return out


def profile_arabic_presence(
    df: pd.DataFrame,
    *,
    columns: Optional[list[str]] = None,
) -> dict[str, float]:
    """
    Detect Arabic-script presence per string column.

    Returns ``{column_name: arabic_fraction}`` for each scanned column.
    """
    target_cols = string_columns(df, columns)
    if not target_cols:
        return {}

    log.debug("Scanning %d columns for Arabic characters...", len(target_cols))
    arabic_columns: dict[str, float] = {}

    for col in target_cols:
        series = df[col].dropna().astype(str)
        if series.empty:
            arabic_columns[col] = 0.0
            continue
        matches = series.apply(lambda v: bool(_ARABIC_PATTERN.search(v)))
        arabic_columns[col] = float(matches.mean())

    flagged = [c for c, f in arabic_columns.items() if f >= 0.05]
    log.info(
        "Arabic detection complete. %d column(s) with Arabic content (sample threshold 5%%): %s",
        len(flagged),
        flagged or "none",
    )
    return arabic_columns


def compute_column_profiles(
    df: pd.DataFrame,
    *,
    arabic_columns: Optional[dict[str, float]] = None,
    threshold: float = 0.30,
    sample_size: int = 5,
    columns: Optional[list[str]] = None,
) -> list[ColumnProfile]:
    """
    Compute PII triage ``ColumnProfile`` rows for string columns.

    Weights: name=0.40, card=0.20, len=0.20, null=0.10, arabic=0.10.
    Returns profiles sorted by ``triage_score`` descending.
    """
    if df is None or df.empty:
        log.warning("No DataFrame available for triage signals.")
        return []

    arabic_columns = arabic_columns or {}
    pdf = df
    n_rows = max(len(pdf), 1)
    string_cols = string_columns(pdf, columns)
    profiles: list[ColumnProfile] = []

    for col in string_cols:
        series = pdf[col]
        non_null = series.dropna().astype(str)
        null_rate = float(series.isna().mean())
        unique_count = int(series.nunique(dropna=True))
        cardinality = unique_count / n_rows
        avg_len = float(non_null.str.len().mean()) if not non_null.empty else 0.0
        physical_type, logical_type, type_source = infer_column_types_from_series(series)

        if cardinality >= 0.10:
            card_score = min(cardinality * 2, 1.0)
        elif cardinality < 0.01:
            card_score = 0.05
        else:
            card_score = cardinality * 5

        if 5 <= avg_len <= 80:
            len_score = min((avg_len - 5) / 75, 1.0)
        elif avg_len < 5:
            len_score = 0.05
        else:
            len_score = max(1.0 - (avg_len - 80) / 200, 0.2)

        null_penalty = max(0.0, 1.0 - null_rate * 2)

        col_lower = col.lower()
        hint_matches = sum(1 for h in _PII_HINTS_LOWER if h in col_lower)
        name_score = min(hint_matches * 1.0, 1.0)

        arabic_frac = arabic_columns.get(col, 0.0)
        arabic_score = min(arabic_frac * 5, 1.0)

        triage_score = (
            card_score * 0.20
            + len_score * 0.20
            + null_penalty * 0.10
            + name_score * 0.40
            + arabic_score * 0.10
        )

        samples = (
            non_null.sample(min(sample_size, len(non_null)), random_state=42).tolist()
            if not non_null.empty
            else []
        )

        profiles.append(
            ColumnProfile(
                column=col,
                dtype=str(series.dtype),
                physical_type=physical_type,
                logical_type=logical_type,
                type_source=type_source,
                cardinality_ratio=round(cardinality, 4),
                avg_value_length=round(avg_len, 2),
                null_rate=round(null_rate, 4),
                name_hint_score=round(name_score, 2),
                arabic_fraction=round(arabic_frac, 4),
                triage_score=round(triage_score, 4),
                send_to_detector=triage_score >= threshold,
                sample_values=[str(s) for s in samples],
            )
        )

    profiles.sort(key=lambda p: p.triage_score, reverse=True)
    scan_count = sum(1 for p in profiles if p.send_to_detector)
    log.info(
        "Triage complete: %d/%d columns flagged for detection (threshold=%.2f).",
        scan_count,
        len(profiles),
        threshold,
    )
    for p in profiles:
        _emit_triage_decision(p, threshold)
    return profiles


def _emit_triage_decision(p: ColumnProfile, threshold: float) -> None:
    from redibis.obs import DecisionRecord, decision

    decision(
        DecisionRecord(
            stage="profiling",
            fn="profiling.triage.compute_profiles",
            table="",
            column=p.column,
            verdict="scan" if p.send_to_detector else "skip",
            confidence=p.triage_score,
            rule=f"triage_score >= {threshold:g}",
            inputs={
                "triage_score": p.triage_score,
                "threshold": threshold,
                "cardinality_ratio": p.cardinality_ratio,
            },
        )
    )


def write_triage_report(
    profiles: list[ColumnProfile],
    output_filename: str,
    *,
    open_browser: bool = False,
) -> str:
    """Write a PII triage HTML table (engine-neutral)."""
    import os
    import webbrowser

    from redibis.profiling.column_types import format_type_cell

    rows = ""
    for p in profiles:
        flag_bg = "#d1fae5" if p.send_to_detector else "#f1f5f9"
        flag_txt = "SCAN" if p.send_to_detector else "skip"
        flag_col = "#065f46" if p.send_to_detector else "#64748b"
        ar_badge = (
            f'<span style="background:#fef3c7;color:#92400e;'
            f'padding:2px 6px;border-radius:3px;font-size:11px;">'
            f"AR {p.arabic_fraction:.0%}</span>"
            if p.arabic_fraction >= 0.05 else ""
        )
        samples = " · ".join(f"<em>{s[:30]}</em>" for s in p.sample_values[:3])
        rows += (
            f"<tr style='background:{flag_bg}'>"
            f"<td style='font-weight:600'>{p.column}</td>"
            f"{format_type_cell(p)}"
            f"<td style='color:{flag_col};font-weight:700'>{flag_txt}</td>"
            f"<td>{p.triage_score:.2f}</td>"
            f"<td>{p.cardinality_ratio:.3f}</td>"
            f"<td>{p.avg_value_length:.1f}</td>"
            f"<td>{p.null_rate:.1%}</td>"
            f"<td>{p.name_hint_score:.2f}</td>"
            f"<td>{ar_badge}</td>"
            f"<td style='font-size:11px;color:#64748b'>{samples}</td>"
            f"</tr>\n"
        )

    html = _TRIAGE_HTML.replace("ROWS_PLACEHOLDER", rows)
    output_path = os.path.abspath(output_filename)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    if open_browser:
        webbrowser.open(f"file://{output_path}")
    return output_path


_TRIAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>PII Triage Report</title>
  <style>
    body { font-family: Arial, sans-serif; background: #f8fafc; padding: 2rem; }
    h1 { color: #1e293b; font-size: 1.5rem; margin-bottom: .25rem; }
    p.sub { color: #64748b; font-size: .85rem; margin-bottom: 1.5rem; }
    table { border-collapse: collapse; width: 100%; background: #fff;
            border-radius: 8px; overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,.1); }
    th { background: #1e293b; color: #fff; padding: .6rem .8rem;
         text-align: left; font-size: .75rem; letter-spacing: .05em;
         text-transform: uppercase; white-space: nowrap; }
    td { padding: .5rem .8rem; font-size: .82rem;
         border-bottom: 1px solid #f1f5f9; }
    tr:last-child td { border-bottom: none; }
  </style>
</head>
<body>
  <h1>PII Column Triage Report</h1>
  <p class="sub">Columns scored by structural signals.
     <strong style="color:#065f46">SCAN</strong> columns are forwarded to Presidio + GLiNER.
  </p>
  <table>
    <thead><tr>
      <th>Column</th><th>Type</th><th>Flag</th><th>Score</th>
      <th>Card.</th><th>Avg Len</th><th>Null %</th><th>Name</th>
      <th>Arabic</th><th>Samples</th>
    </tr></thead>
    <tbody>ROWS_PLACEHOLDER</tbody>
  </table>
</body>
</html>"""
