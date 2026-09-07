"""
Export PII detection results to HTML reports.
"""

from typing import List
import html as _html
import os
from redibis.models import PIIDetection


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1%}"


def _evidence_card(d: PIIDetection) -> str:
    """Expandable per-column evidence: which regex/NER fired, score, match-rate."""
    hits = getattr(d, "regex_hits", None) or []
    ner = getattr(d, "ner_hits", None) or []
    if not hits and not ner:
        return ""

    def esc(value: object) -> str:
        return _html.escape(str(value if value is not None else ""))

    def pct(value: object) -> str:
        try:
            return f"{float(value) * 100:.0f}%"
        except Exception:
            return "—"

    rrows = "".join(
        "<tr>"
        f"<td>{esc(hit.get('pattern_name'))}</td>"
        f"<td><code style='font-size:.8em'>{esc(hit.get('regex'))}</code></td>"
        f"<td>{esc(hit.get('entity_type'))}</td>"
        f"<td style='text-align:center'>{esc(hit.get('score'))}</td>"
        f"<td style='text-align:center'>{pct(hit.get('match_rate'))}</td>"
        f"<td>{esc(hit.get('collision_group'))}</td>"
        f"<td>{esc(hit.get('validator'))}</td></tr>"
        for hit in hits
    )
    regex_tbl = (
        "<table style='width:100%;border-collapse:collapse;font-size:.82em;margin-top:6px'>"
        "<thead><tr style='text-align:left;color:#64748b'>"
        "<th>Pattern</th><th>Regex</th><th>Entity</th><th>Score</th><th>Match&nbsp;rate</th><th>Collision</th><th>Validator</th>"
        "</tr></thead><tbody>" + rrows + "</tbody></table>"
    ) if rrows else ""

    nrows = "".join(
        "<tr>"
        f"<td>{esc(entry.get('model') or entry.get('ner_engine'))}</td>"
        f"<td>{esc(entry.get('label') or entry.get('entity_type'))}</td>"
        f"<td style='text-align:center'>{esc(entry.get('score'))}</td>"
        f"<td style='text-align:center'>{pct(entry.get('match_rate'))}</td></tr>"
        for entry in ner
    )
    ner_tbl = (
        "<div style='margin-top:8px;color:#64748b;font-size:.8em'>NER hits</div>"
        "<table style='width:100%;border-collapse:collapse;font-size:.82em'>"
        "<thead><tr style='text-align:left;color:#64748b'><th>Model</th><th>Label</th><th>Score</th><th>Match&nbsp;rate</th></tr></thead>"
        "<tbody>" + nrows + "</tbody></table>"
    ) if nrows else ""

    conf = f"{d.confidence:.0%}" if getattr(d, "confidence", None) else ""
    why = esc(getattr(d, "decision_rule", "") or getattr(d, "decision_path", "") or "—")
    return (
        "<details style='margin:6px 0;border:1px solid #e2e8f0;border-radius:8px;padding:8px 12px'>"
        f"<summary style='cursor:pointer;font-weight:600'>{esc(d.column)} "
        f"<span style='color:#64748b;font-weight:400'>· {esc(getattr(d, 'entity_type', ''))} · {conf}</span></summary>"
        f"<div style='font-size:.82em;color:#334155;margin-top:6px'><b>Why:</b> {why}</div>"
        + regex_tbl + ner_tbl + "</details>"
    )


def _engine_state_html(detection: PIIDetection) -> str:
    from redibis.pii.equations import build_column_report

    states = build_column_report(detection).engine_state
    lines: list[str] = []
    for engine_name in ("regex", "ner", "llm", "phone"):
        state = states.get(engine_name, {}) or {}
        status = state.get("status", "not_run")
        score = _fmt_pct(state.get("score"))
        conf = "confident" if state.get("confident") else "not confident"
        extra = ""
        if engine_name == "phone":
            details: list[str] = []
            if state.get("entity_type"):
                details.append(str(state["entity_type"]))
            if state.get("msisdn_valid_rate") is not None:
                details.append(f"msisdn {_fmt_pct(state.get('msisdn_valid_rate'))}")
            if state.get("valid_rate") is not None:
                details.append(f"valid {_fmt_pct(state.get('valid_rate'))}")
            if state.get("mobile_rate") is not None:
                details.append(f"mobile {_fmt_pct(state.get('mobile_rate'))}")
            if state.get("regions"):
                regions = ", ".join(sorted((state.get("regions") or {}).keys()))
                details.append(f"regions {regions}")
            if details:
                extra = " | " + ", ".join(details)
        elif engine_name == "regex" and state.get("pattern"):
            extra = f" | pattern {state['pattern']}"
        elif engine_name == "ner" and state.get("label"):
            extra = f" | label {state['label']}"
        elif engine_name == "llm" and state.get("verdict"):
            extra = f" | verdict {state['verdict']}"
        lines.append(
            f"<div><strong>{engine_name}</strong>: {status} | score {score} | {conf}{extra}</div>"
        )
    return "".join(lines)


def render_pii_detection_report_html(
    detections: List[PIIDetection],
    table_name: str,
    *,
    session_id: str = "",
) -> str:
    """Build PII detection report HTML (in-memory; no file I/O)."""
    detected_rows = ""
    evidence_cards = ""
    clean_rows = ""

    for d in detections:
        if d.detected:
            entities = d.entity_type or "Unknown"
            confidence = f"{d.confidence:.1%}" if d.confidence else "—"
            engines = _engine_state_html(d)

            row = f"""
            <tr style="background:#fef2f2;border-bottom:1px solid #fee2e2">
              <td style="font-weight:600;color:#dc2626">{d.column}</td>
              <td style="color:#dc2626">✓ DETECTED</td>
              <td>{entities}</td>
              <td>{d.classification}</td>
              <td style="font-family:monospace;font-size:0.9em">{engines}</td>
              <td style="text-align:center">{confidence}</td>
              <td style="font-size:0.85em;color:#64748b">{getattr(d, "decision_rule", "") or d.decision_path or "—"}</td>
            </tr>
            """
            detected_rows += row
            evidence_cards += _evidence_card(d)
        else:
            confidence = f"{d.confidence:.1%}" if d.confidence else "—"
            engines = _engine_state_html(d)

            row = f"""
            <tr style="background:#f8fafc;border-bottom:1px solid #e2e8f0">
              <td style="color:#64748b">{d.column}</td>
              <td style="color:#22c55e">✓ CLEAN</td>
              <td>—</td>
              <td>—</td>
              <td style="font-family:monospace;font-size:0.9em;color:#64748b">{engines}</td>
              <td style="text-align:center;color:#64748b">{confidence}</td>
              <td style="font-size:0.85em;color:#64748b">{getattr(d, "decision_rule", "") or d.decision_path or "—"}</td>
            </tr>
            """
            clean_rows += row

    total = len(detections)
    confirmed = sum(1 for d in detections if d.detected)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>PII Detection Report — {table_name}</title>
  <style>
    :root {{
      --ink: #0f172a;
      --red: #dc2626;
      --green: #22c55e;
      --amber: #f59e0b;
      --bg: #f8fafc;
      --surface: #fff;
      --border: #e2e8f0;
      --muted: #64748b;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
      padding-bottom: 60px;
    }}
    .mono {{ font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; }}
    .header {{
      background: var(--ink);
      color: #fff;
      padding: 2rem;
      border-bottom: 1px solid var(--border);
    }}
    .header h1 {{
      font-size: 1.8rem;
      font-weight: 700;
      margin-bottom: 0.5rem;
    }}
    .header p {{
      color: #cbd5e1;
      font-size: 0.95rem;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1px;
      background: var(--border);
      margin: 2rem;
    }}
    .stat {{
      background: var(--surface);
      padding: 1.5rem;
      text-align: center;
    }}
    .stat-value {{
      font-size: 2.2rem;
      font-weight: 700;
      margin-bottom: 0.5rem;
    }}
    .stat-label {{
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      font-weight: 600;
    }}
    .stat-value.confirmed {{ color: var(--red); }}
    .stat-value.clean {{ color: var(--green); }}
    .content {{
      margin: 2rem;
    }}
    .section-title {{
      font-size: 1.1rem;
      font-weight: 700;
      margin: 2rem 0 1rem 0;
      padding-bottom: 0.75rem;
      border-bottom: 2px solid var(--border);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--surface);
      border: 1px solid var(--border);
      margin-bottom: 2rem;
    }}
    thead {{
      background: #f1f5f9;
    }}
    th {{
      padding: 0.75rem 1rem;
      text-align: left;
      font-weight: 600;
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      border-bottom: 2px solid var(--border);
    }}
    td {{
      padding: 0.75rem 1rem;
    }}
    .footer {{
      margin: 3rem 2rem;
      padding-top: 1rem;
      border-top: 1px solid var(--border);
      font-size: 0.85rem;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <div class="header">
    <h1>PII Detection Report</h1>
    <p>{table_name}</p>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-value">{total}</div>
      <div class="stat-label">Total Columns</div>
    </div>
    <div class="stat">
      <div class="stat-value confirmed">{confirmed}</div>
      <div class="stat-label">PII Detected</div>
    </div>
    <div class="stat">
      <div class="stat-value clean">{total - confirmed}</div>
      <div class="stat-label">Clean</div>
    </div>
  </div>

  <div class="content">
    {f'<div class="section-title">🔴 Detected PII ({confirmed})</div>' if detected_rows else ''}
    {f'<table><thead><tr><th>Column</th><th>Status</th><th>Entity Type</th><th>Classification</th><th>Engines</th><th>Confidence</th><th>Decision Path</th></tr></thead><tbody>{detected_rows}</tbody></table>' if detected_rows else ''}
    {f'<div class="section-title">🔍 Detection Evidence</div>{evidence_cards}' if evidence_cards else ''}

    <div class="section-title">✓ Clean Columns ({total - confirmed})</div>
    <table>
      <thead>
        <tr>
          <th>Column</th>
          <th>Status</th>
          <th>Entity Type</th>
          <th>Classification</th>
          <th>Engines</th>
          <th>Confidence</th>
          <th>Decision Path</th>
        </tr>
      </thead>
      <tbody>
        {clean_rows}
      </tbody>
    </table>
  </div>

  <div class="footer">
    <p>Generated PII Detection Report {f'(Session: {session_id})' if session_id else ''}</p>
  </div>
</body>
</html>
"""


def export_pii_detection_report(
    detections: List[PIIDetection],
    table_name: str,
    output_filename: str = "pii_detection_report.html",
    session_id: str = "",
) -> str:
    """
    Export PII detection results to an HTML report.

    Returns:
        Path to the generated HTML file
    """
    output_path = os.path.abspath(output_filename)
    html = render_pii_detection_report_html(
        detections, table_name, session_id=session_id,
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path
