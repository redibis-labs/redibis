"""Standalone HTML for text-span evaluation reports (no CDN, escaped text)."""

from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

from redibis.pii.eval.span_metrics import (
    BATCH_REPORT_KIND,
    REPORT_KIND,
    SUPPORTED_REPORT_VERSIONS,
)

MAX_CASE_TEXT = 8000
_CLASS_TINT = {
    "exact": "exact",
    "equivalent": "exact",
    "superset": "near",
    "subset": "near",
    "overlap_partial": "near",
    "type_mismatch": "bad",
    "split": "near",
    "merged": "near",
    "missed": "miss",
    "spurious": "bad",
    "guard_violation": "bad",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _case_text(text: str) -> str:
    raw = str(text or "")
    if len(raw) <= MAX_CASE_TEXT:
        return raw
    return raw[:MAX_CASE_TEXT] + "…"


def _micro_line(block: Mapping[str, Any] | None) -> str:
    micro = (block or {}).get("micro") or block or {}
    if "accuracy" in micro and "f1" not in micro:
        return (
            f"acc {_esc(micro.get('accuracy', 0))} · "
            f"paired {_esc(micro.get('paired', 0))} · "
            f"match {_esc(micro.get('type_match', 0))} / "
            f"mismatch {_esc(micro.get('type_mismatch', 0))}"
        )
    extra = ""
    if "accuracy" in micro:
        extra = f" · acc {_esc(micro.get('accuracy', 0))}"
    return (
        f"F1 {_esc(micro.get('f1', 0))} · "
        f"P {_esc(micro.get('precision', 0))} · "
        f"R {_esc(micro.get('recall', 0))}{extra} · "
        f"TP {_esc(micro.get('tp', 0))} / FP {_esc(micro.get('fp', 0))} / FN {_esc(micro.get('fn', 0))}"
    )


def _provenance(report: Mapping[str, Any]) -> str:
    prov = report.get("provenance") or {}
    pack = prov.get("pack_stack") or {}
    pack_uuid = pack.get("stack_uuid") if isinstance(pack, Mapping) else ""
    layers = []
    if isinstance(pack, Mapping):
        for row in pack.get("packs") or []:
            layers.append(f"{row.get('id') or ''}@{row.get('version') or ''}")
    items = [
        ("run", prov.get("run_uuid") or "—"),
        ("redibis", report.get("redibis_version") or prov.get("redibis_version") or "—"),
        ("pack stack", pack_uuid or "—"),
        ("layers", ", ".join(x for x in layers if x.strip("@")) or "—"),
        ("rules", prov.get("rules_checksum") or "—"),
        ("rules source", prov.get("rules_source") or "—"),
        ("normalization", report.get("normalization_profile") or prov.get("normalization_profile") or "v1"),
        ("engines ran", ", ".join(prov.get("engines_ran") or []) or "—"),
        ("unavailable", ", ".join(f"{k}: {v}" for k, v in (prov.get("engines_unavailable") or {}).items()) or "—"),
        ("NER", f"{prov.get('ner_backend') or '—'} {prov.get('ner_model_id') or ''}".strip()),
        ("NER digest", prov.get("ner_model_sha256") or "—"),
        ("regex inventory", str(len(prov.get("regex_inventory") or []))),
        ("expanders", ", ".join(prov.get("preprocess_expanders") or []) or "—"),
    ]
    if prov.get("rules_unpinned") or prov.get("warning"):
        items.append(("warning", prov.get("warning") or "unpinned rules (config+stored)"))
    if prov.get("provenance_degraded"):
        items.append(("degraded", prov.get("provenance_degraded_reason") or "yes"))
    gates = report.get("gates") or {}
    if gates:
        items.append(("gate", gates.get("summary") or ("pass" if gates.get("passed") else "fail")))
    rows = "".join(
        f"<div><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>" for k, v in items
    )
    return f'<dl class="prov">{rows}</dl>'


def _notes_html(report: Mapping[str, Any]) -> str:
    notes = dict(report.get("notes") or {})
    if not notes:
        notes = {
            "guard_violations": (
                "A forbidden-span hit is counted as class=guard_violation and as a "
                "false positive on every scored tier."
            )
        }
    items = "".join(
        f"<div><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>" for k, v in notes.items()
    )
    return f'<dl class="prov">{items}</dl>'


def _kpi_tiles(report: Mapping[str, Any]) -> str:
    deltas = (report.get("gates") or {}).get("deltas") or {}
    tiles = []
    for key, label in (("strict", "strict F1"), ("value", "value F1"), ("overlap", "overlap F1"), ("type", "type")):
        block = report.get(key) or report.get("exact" if key == "strict" else "") or {}
        micro = (block or {}).get("micro") or {}
        value = micro.get("accuracy") if key == "type" and "accuracy" in micro else micro.get("f1", 0)
        delta = deltas.get(f"{key}_f1")
        delta_html = f'<span class="delta">Δ {_esc(delta)}</span>' if delta is not None else ""
        tiles.append(
            f'<div class="tile"><div class="lbl">{_esc(label)}</div>'
            f'<div class="num">{_esc(value)}</div>{delta_html}</div>'
        )
    return '<div class="kpis">' + "".join(tiles) + "</div>"


def _class_bar(dist: Mapping[str, Any] | None) -> str:
    counts = dict(dist or {})
    total = sum(int(v or 0) for v in counts.values()) or 1
    parts = []
    for name in (
        "exact", "equivalent", "superset", "subset", "overlap_partial",
        "type_mismatch", "split", "merged", "missed", "spurious", "guard_violation",
    ):
        n = int(counts.get(name) or 0)
        pct = 100 * n / total
        parts.append(
            f'<div class="bar-row"><span class="cn">{_esc(name)}</span>'
            f'<span class="track"><i class="fill {_esc(_CLASS_TINT.get(name, "near"))}" '
            f'style="width:{pct:.1f}%"></i></span>'
            f"<span>{n}</span></div>"
        )
    return '<div class="classbar">' + "".join(parts) + "</div>"


def _char_diff(expected: str, predicted: str) -> str:
    # Simple prefix/suffix mark-up; escaped.
    i = 0
    while i < min(len(expected), len(predicted)) and expected[i] == predicted[i]:
        i += 1
    j = 0
    while (
        j < min(len(expected), len(predicted)) - i
        and expected[len(expected) - 1 - j] == predicted[len(predicted) - 1 - j]
    ):
        j += 1
    common_l = _esc(expected[:i])
    exp_mid = _esc(expected[i: len(expected) - j if j else len(expected)])
    pred_mid = _esc(predicted[i: len(predicted) - j if j else len(predicted)])
    common_r = _esc(expected[len(expected) - j:] if j else "")
    return (
        f'<span class="diff">{common_l}<del>{exp_mid}</del>'
        f"<ins>{pred_mid}</ins>{common_r}</span>"
    )


def _overlay_from_classes(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    text = str(case.get("text") or "")
    n = len(text)
    labels = [""] * n
    extra = [False] * n
    rows = list(case.get("match_classes") or [])
    expected = list(case.get("expected_spans") or [])
    predicted = list(case.get("predicted_spans") or [])
    rank = {"bad": 5, "miss": 4, "near": 3, "exact": 2, "extra": 6}

    def paint(start: int, end: int, kind: str, is_extra: bool = False) -> None:
        s = max(0, min(n, start))
        e = max(0, min(n, end))
        for i in range(s, e):
            if is_extra:
                extra[i] = True
            if rank.get(kind, 0) >= rank.get(labels[i], 0):
                labels[i] = kind

    if rows:
        for row in rows:
            cls = str(row.get("class") or "missed")
            tint = _CLASS_TINT.get(cls, "near")
            ei = row.get("expected_index")
            pi = row.get("predicted_index")
            if ei is not None and ei < len(expected):
                gold = expected[ei]
                paint(int(gold["start"]), int(gold["end"]), "miss" if cls == "missed" else tint)
            if pi is not None and pi < len(predicted):
                pred = predicted[pi]
                paint(int(pred["start"]), int(pred["end"]), tint)
                if ei is not None and ei < len(expected) and cls in {"superset", "equivalent", "overlap_partial"}:
                    gold = expected[ei]
                    if int(pred["start"]) < int(gold["start"]):
                        paint(int(pred["start"]), int(gold["start"]), tint, True)
                    if int(pred["end"]) > int(gold["end"]):
                        paint(int(gold["end"]), int(pred["end"]), tint, True)
        out = []
        for i, kind in enumerate(labels):
            if kind:
                out.append({"start": i, "end": i + 1, "kind": "extra" if extra[i] else kind})
        return out
    # fallback to exact/overlap match lists
    metric = case.get("exact") or {}
    matched_expected = {m["expected_index"] for m in metric.get("matches") or []}
    matched_predicted = {m["predicted_index"] for m in metric.get("matches") or []}
    out: list[dict[str, Any]] = []
    for index, span in enumerate(case.get("expected_spans") or []):
        out.append({**dict(span), "kind": "exact" if index in matched_expected else "miss"})
    for index, span in enumerate(case.get("predicted_spans") or []):
        if index not in matched_predicted:
            out.append({**dict(span), "kind": "bad"})
    for span in case.get("proposal_spans") or []:
        out.append({**dict(span), "kind": "proposal"})
    return out


def _highlight_html(text: str, spans: Sequence[Mapping[str, Any]]) -> str:
    chars = list(text or "")
    n = len(chars)
    labels = [""] * n
    rank = {"extra": 6, "bad": 5, "miss": 4, "near": 3, "exact": 2, "fp": 5, "fn": 4, "tp": 2, "proposal": 1}
    for span in spans:
        try:
            start = max(0, int(span.get("start") or 0))
            end = min(n, int(span.get("end") or 0))
        except (TypeError, ValueError):
            continue
        kind = str(span.get("kind") or "exact")
        for i in range(start, end):
            if rank.get(kind, 0) >= rank.get(labels[i], 0):
                labels[i] = kind
    parts: list[str] = []
    i = 0
    while i < n:
        j = i + 1
        while j < n and labels[j] == labels[i]:
            j += 1
        chunk = _esc("".join(chars[i:j]))
        kind = labels[i]
        if kind:
            parts.append(f'<mark class="hl {kind}">{chunk}</mark>')
        else:
            parts.append(chunk)
        i = j
    return "".join(parts) or "&nbsp;"


def _slice(text: str, span: Mapping[str, Any] | None) -> str:
    if not span:
        return ""
    try:
        return (text or "")[int(span["start"]): int(span["end"])]
    except (TypeError, ValueError, KeyError):
        return ""


def _span_table(case: Mapping[str, Any]) -> str:
    text = str(case.get("text") or "")
    expected = list(case.get("expected_spans") or [])
    predicted = list(case.get("predicted_spans") or [])
    rows_html = []
    for row in case.get("match_classes") or []:
        ei = row.get("expected_index")
        pi = row.get("predicted_index")
        gold = expected[ei] if ei is not None and ei < len(expected) else None
        pred = predicted[pi] if pi is not None and pi < len(predicted) else None
        exp_txt = _slice(text, gold)
        pred_txt = _slice(text, pred)
        cov = row.get("coverage")
        prec = row.get("char_precision")
        type_ok = "✓" if (row.get("expected_type") and row.get("expected_type") == row.get("got_type")) else "✗"
        if row.get("class") in {"missed"}:
            type_ok = "—"
        type_cell = f"{type_ok} {_esc(row.get('expected_type') or '')}"
        if row.get("class") == "type_mismatch":
            type_cell = f"✗ {_esc(row.get('expected_type'))} → {_esc(row.get('got_type'))}"
        class_label = str(row.get("class") or "")
        if row.get("value_equal"):
            class_label = f"{class_label} =value"
        tint = _CLASS_TINT.get(str(row.get("class")), "near")
        if row.get("value_equal") and str(row.get("class")) in {"exact", "equivalent"}:
            tint = "exact"
        engine = ""
        if pred:
            engine = " · ".join(str(x) for x in (pred.get("engine"), pred.get("recognizer")) if x)
        score = pred.get("score") if pred else ""
        rows_html.append(
            "<tr>"
            f"<td><code>{_esc(exp_txt[:80])}</code></td>"
            f"<td><code>{_esc(pred_txt[:80])}</code></td>"
            f'<td><span class="pill {_esc(tint)}">{_esc(class_label)}</span></td>'
            f"<td>{_esc('' if cov is None else f'{100 * float(cov):.0f}%')}</td>"
            f"<td>{_esc('' if prec is None else f'{100 * float(prec):.0f}%')}</td>"
            f"<td>{_esc(row.get('delta_label') or '—')}</td>"
            f"<td>{type_cell}</td>"
            f"<td>{_esc(score)}</td>"
            f"<td>{_esc(engine)}</td>"
            "</tr>"
            f'<tr class="why"><td colspan="9">{_char_diff(exp_txt, pred_txt)}'
            f' · grade {_esc(row.get("grade") or "strict")}'
            + (f' · { _esc(row.get("reason"))}' if row.get("reason") else "")
            + "</td></tr>"
        )
    if not rows_html:
        return ""
    return (
        "<table class=\"spans\"><thead><tr>"
        "<th>Expected</th><th>Predicted</th><th>Class</th><th>Coverage</th>"
        "<th>Char prec.</th><th>Δ chars</th><th>Type</th><th>Score</th><th>Engine</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )


def _case_html(case: Mapping[str, Any], *, metric_name: str) -> str:
    exact = case.get("exact") or case.get("strict") or {}
    value = case.get("value") or {}
    overlap = case.get("overlap") or {}
    overlays = _overlay_from_classes(case)
    return (
        '<article class="case">'
        f"<h3>{_esc(case.get('id') or 'case')}</h3>"
        f'<p class="meta">language {_esc(case.get("language") or "en")} · '
        f"tags {_esc(', '.join(case.get('tags') or []) or '—')} · "
        f"strict F1 {_esc(exact.get('f1', 0))} · value F1 {_esc(value.get('f1', 0))} · "
        f"overlap F1 {_esc(overlap.get('f1', 0))}</p>"
        f'<pre class="text">{_highlight_html(_case_text(str(case.get("text") or "")), overlays)}</pre>'
        + _span_table(case)
        + "</article>"
    )


def _css() -> str:
    return """
body{font:14px/1.45 system-ui,sans-serif;color:#111;background:#fff;margin:24px}
h1,h2,h3{font-weight:650;margin:0 0 8px}
.meta,.muted{color:#555}
.card{border:1px solid #ddd;border-radius:8px;padding:16px;margin:0 0 16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:0 0 16px}
.tile{border:1px solid #ddd;border-radius:8px;padding:12px}
.tile .lbl{color:#555;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.tile .num{font-size:22px;font-weight:700}
.delta{font-size:12px;color:#555}
.file.failed{border-color:#c1272d}
.hl{border-radius:3px;padding:0 1px;border-bottom:2px solid}
.hl.exact{background:#dcfce7;border-color:#166534}
.hl.near{background:#fef3c7;border-color:#b45309}
.hl.bad{background:#fee2e2;border-color:#c1272d}
.hl.miss{background:repeating-linear-gradient(45deg,#e5e7eb,#e5e7eb 4px,#fff 4px,#fff 8px);border-color:#6b7280;background-color:transparent}
.hl.extra{background:#fde68a;border-color:#b45309}
.hl.fp{background:#fee2e2;border-color:#c1272d}
.hl.fn{background:#ffedd5;border-color:#b45309}
.hl.tp{background:#dcfce7;border-color:#166534}
.hl.proposal{background:transparent;border-bottom-style:dashed;opacity:.85}
pre.text{white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,monospace;margin:8px 0 0}
.legend span{display:inline-block;margin-right:12px}
.legend .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;border-bottom:2px solid}
.prov{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 16px}
.prov dt{color:#555;font-size:12px}
.prov dd{font-family:ui-monospace,monospace;margin:0;word-break:break-all}
.classbar .bar-row{display:grid;grid-template-columns:140px 1fr 40px;gap:8px;align-items:center;margin:4px 0}
.track{background:#f3f4f6;height:8px;border-radius:99px;overflow:hidden}
.fill{display:block;height:8px}
.fill.exact{background:#166534}
.fill.near{background:#b45309}
.fill.bad{background:#c1272d}
.fill.miss{background:#6b7280}
.spans{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px}
.spans th,.spans td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
.pill{display:inline-block;padding:1px 6px;border-radius:99px;font-size:11px}
.pill.exact{background:#dcfce7;color:#166534}
.pill.near{background:#fef3c7;color:#92400e}
.pill.bad{background:#fee2e2;color:#991b1b}
.pill.miss{background:#f3f4f6;color:#374151}
.diff del{background:#fee2e2;text-decoration:none}
.diff ins{background:#dcfce7;text-decoration:none}
.why td{color:#555;font-size:12px}
"""


def render_single_report_html(report: Mapping[str, Any], *, metric_name: str = "exact") -> str:
    cases = "".join(_case_html(case, metric_name=metric_name) for case in report.get("cases") or [])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Redibis evaluation report</title>
<style>{_css()}</style></head>
<body>
<h1>Text-span evaluation</h1>
{_provenance(report)}
{_notes_html(report)}
{_kpi_tiles(report)}
<div class="card grid">
  <div><strong>Strict</strong><div>{_micro_line(report.get("strict") or report.get("exact"))}</div></div>
  <div><strong>Value</strong><div>{_micro_line(report.get("value"))}</div></div>
  <div><strong>Overlap</strong><div>{_micro_line(report.get("overlap"))}</div></div>
  <div><strong>Type</strong><div>{_micro_line(report.get("type"))}</div></div>
</div>
<div class="card"><h2>Class distribution</h2>{_class_bar(report.get("class_distribution"))}</div>
<p class="legend muted">
  <span><i class="sw" style="background:#dcfce7;border-color:#166534"></i>exact / equivalent</span>
  <span><i class="sw" style="background:#fef3c7;border-color:#b45309"></i>near-miss</span>
  <span><i class="sw" style="background:#fde68a;border-color:#b45309"></i>over-captured chars</span>
  <span><i class="sw" style="background:#fee2e2;border-color:#c1272d"></i>mismatch / spurious</span>
  <span><i class="sw" style="border-bottom:2px dashed #555"></i>missed</span>
</p>
{cases or '<p class="muted">No cases.</p>'}
</body></html>
"""


def render_batch_report_html(report: Mapping[str, Any], *, metric_name: str = "exact") -> str:
    files_html: list[str] = []
    for entry in report.get("files") or []:
        status = entry.get("status")
        body = ""
        if status == "evaluated":
            body = "".join(
                _case_html(case, metric_name=metric_name) for case in entry.get("cases") or []
            )
        else:
            body = f'<p class="muted">Skipped: {_esc(entry.get("error") or "failed")}</p>'
        files_html.append(
            f'<section class="card file { _esc(status) }">'
            f"<h2>{_esc(entry.get('name') or entry.get('path'))}</h2>"
            f'<p class="meta">{_esc(status)} · {_esc(entry.get("dataset_id"))} · '
            f"{_esc(entry.get('case_count') or 0)} case(s)"
            + (f' · {_micro_line(entry.get("exact"))}' if status == "evaluated" else "")
            + "</p>"
            + body
            + "</section>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Redibis batch evaluation report</title>
<style>{_css()}</style></head>
<body>
<h1>Batch text-span evaluation</h1>
{_provenance(report)}
{_notes_html(report)}
{_kpi_tiles(report)}
<div class="card grid">
  <div><strong>Overall strict</strong><div>{_micro_line(report.get("strict") or report.get("exact"))}</div></div>
  <div><strong>Overall value</strong><div>{_micro_line(report.get("value"))}</div></div>
  <div><strong>Overall overlap</strong><div>{_micro_line(report.get("overlap"))}</div></div>
  <div><strong>Overall type</strong><div>{_micro_line(report.get("type"))}</div></div>
</div>
<div class="card"><h2>Class distribution</h2>{_class_bar(report.get("class_distribution"))}</div>
<p class="muted">{_esc(report.get("evaluated_count"))} evaluated ·
{_esc(report.get("failed_count"))} failed · {_esc(report.get("case_count"))} cases</p>
{''.join(files_html) or '<p class="muted">No files.</p>'}
</body></html>
"""


def render_report_html(report: Mapping[str, Any], *, metric_name: str = "exact") -> str:
    if metric_name not in ("exact", "overlap", "strict", "value"):
        raise ValueError(f"unsupported metric {metric_name!r}")
    version = str(report.get("schema_version") or "")
    if version not in SUPPORTED_REPORT_VERSIONS:
        raise ValueError(f"unsupported schema_version {report.get('schema_version')!r}")
    kind = report.get("kind")
    if kind == BATCH_REPORT_KIND:
        return render_batch_report_html(report, metric_name=metric_name)
    if kind == REPORT_KIND:
        return render_single_report_html(report, metric_name=metric_name)
    raise ValueError(f"unsupported report kind {kind!r}")
