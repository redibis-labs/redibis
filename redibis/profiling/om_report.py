"""HTML report renderer for OpenMetadata-style column metrics."""

from __future__ import annotations

import html
from typing import Optional

from redibis.profiling.om_metrics import ColumnMetric


def render_metrics_report(
    metrics: dict[str, ColumnMetric],
    *,
    dataset_name: str,
    title: Optional[str] = None,
) -> str:
    """Self-contained HTML column profile report."""
    report_title = title or f"Column Profile — {dataset_name}"
    rows = ""
    for col in sorted(metrics.keys()):
        m = metrics[col]
        freq = ", ".join(
            f"{html.escape(str(v))} ({c})" for v, c in m.frequent_values[:5]
        ) or "—"
        hist = ", ".join(
            f"{html.escape(str(h.get('bucket', '')))}: {h.get('count', 0)}"
            for h in m.histogram[:5]
        ) or "—"
        rows += (
            "<tr>"
            f"<td><strong>{html.escape(col)}</strong></td>"
            f"<td>{html.escape(m.data_type)}</td>"
            f"<td>{m.null_proportion:.1%}</td>"
            f"<td>{m.unique_count} ({m.unique_proportion:.1%})</td>"
            f"<td>{html.escape(str(m.min_value)) if m.min_value is not None else '—'}</td>"
            f"<td>{html.escape(str(m.max_value)) if m.max_value is not None else '—'}</td>"
            f"<td>{m.mean if m.mean is not None else '—'}</td>"
            f"<td>{m.avg_length if m.avg_length is not None else '—'}</td>"
            f"<td style='font-size:11px'>{freq}</td>"
            f"<td style='font-size:11px'>{hist}</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(report_title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #0f172a; }}
    h1 {{ font-size: 1.25rem; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border: 1px solid #e2e8f0; padding: 8px; text-align: left; }}
    th {{ background: #f8fafc; }}
    tr:nth-child(even) {{ background: #fafafa; }}
  </style>
</head>
<body>
  <h1>{html.escape(report_title)}</h1>
  <p>Engine: open_metadata · Columns: {len(metrics)}</p>
  <table>
    <thead>
      <tr>
        <th>Column</th><th>Type</th><th>Null %</th><th>Distinct</th>
        <th>Min</th><th>Max</th><th>Mean</th><th>Avg len</th>
        <th>Top values</th><th>Histogram</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""
