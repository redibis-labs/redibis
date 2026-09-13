"""Standalone HTML for structured column evaluation reports."""

from __future__ import annotations

import html
from typing import Any, Mapping

from redibis.evaluation.schema import REPORT_KIND, SCHEMA_VERSION


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_table_report_html(report: Mapping[str, Any]) -> str:
    if report.get("kind") != REPORT_KIND:
        raise ValueError(f"unsupported report kind {report.get('kind')!r}")
    if str(report.get("schema_version") or "") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {report.get('schema_version')!r}")
    rows = []
    for col in report.get("columns") or []:
        fields = col.get("fields") or {}
        pii = fields.get("is_pii") or {}
        entity = fields.get("entity_type") or {}
        rows.append(
            "<tr>"
            f"<td class='mono'>{_esc(col.get('name'))}</td>"
            f"<td>{_esc(pii.get('expected'))} → {_esc(pii.get('actual'))}</td>"
            f"<td>{_esc(entity.get('expected'))} → {_esc(entity.get('actual'))}</td>"
            f"<td>{_esc((col.get('exact') or {}).get('f1'))}</td>"
            f"<td>{_esc(col.get('source'))}{' proposal' if col.get('is_proposal') else ''}</td>"
            "</tr>"
        )
    micro = (report.get("exact") or {}).get("micro") or {}
    pii = (report.get("pii") or {}).get("micro") or {}
    provenance = report.get("provenance") or {}
    coverage = report.get("coverage") or {}
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Redibis table evaluation</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#111;background:#fff}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
.mono{{font-family:ui-monospace,monospace}}
.muted{{color:#555}}
.card{{border:1px solid #ddd;border-radius:8px;padding:16px;margin:0 0 16px}}
</style></head>
<body>
<h1>Table-column evaluation</h1>
<p class="muted">{_esc(report.get('kind'))} · Redibis {_esc(report.get('redibis_version'))} ·
target {_esc(report.get('target'))} · table {_esc(report.get('table_name'))}</p>
<div class="card">Exact F1 {_esc(micro.get('f1'))} · PII F1 {_esc(pii.get('f1'))} ·
missing {_esc(', '.join(report.get('missing_columns') or []) or 'none')} ·
extra {_esc(', '.join(report.get('extra_columns') or []) or 'none')} ·
target missing {_esc(', '.join(report.get('actual_missing_columns') or []) or 'none')} ·
target extra {_esc(', '.join(report.get('actual_extra_columns') or []) or 'none')} ·
coverage {_esc(coverage.get('present', 0))}/{_esc(coverage.get('expected', 0))} ·
engines {_esc(provenance.get('engines') or 'n/a')} ·
equation {_esc(provenance.get('equation') or 'n/a')}</div>
<table><thead><tr><th>Column</th><th>PII</th><th>Entity</th><th>F1</th><th>Source</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="5">No columns.</td></tr>'}</tbody></table>
</body></html>
"""
