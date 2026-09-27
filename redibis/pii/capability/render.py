"""HTML / PDF rendering of the PII capability contract (see ``redibis.pii.capability``)."""

from __future__ import annotations

import glob
import html
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_STATUS = {
    "passed": ("✓ verified", "ok"),
    "failed": ("✗ does not hold", "bad"),
    "not_verified": ("— not verified here", "muted"),
    None: ("not run", "muted"),
}

_CSS = """
:root{--ink:#1f2328;--muted:#6b7280;--line:#d8dde3;--ok:#1e7a46;--okbg:#e8f5ec;--bad:#b42318;
 --badbg:#fdecea;--warn:#8a5a00;--warnbg:#fff4de;--head:#f4f6f8;--accent:#5c1a1a}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font:13px/1.5 "DejaVu Sans","Segoe UI",Arial,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 28px}
h1{font-size:26px;margin:0 0 4px;color:var(--accent)} h2{font-size:18px;margin:28px 0 8px;border-bottom:2px solid var(--line);padding-bottom:4px}
h3{font-size:15px;margin:20px 0 6px} p{margin:6px 0} .muted{color:var(--muted)} .small{font-size:11px}
table{width:100%;border-collapse:collapse;margin:8px 0 14px;font-size:12px}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}
th{background:var(--head);font-weight:600}
code,.mono{font-family:"DejaVu Sans Mono",Consolas,monospace;font-size:11.5px}
.vals{white-space:pre-wrap;word-break:break-all}
.chip{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:600;white-space:nowrap}
.ok{background:var(--okbg);color:var(--ok)} .bad{background:var(--badbg);color:var(--bad)}
.muted.chip{background:#eef0f2;color:var(--muted)} .warn{background:var(--warnbg);color:var(--warn)}
.cover{border:2px solid var(--accent);border-radius:10px;padding:16px 20px;margin:14px 0 20px}
.kpis{display:flex;gap:18px;flex-wrap:wrap;margin-top:8px} .kpi b{font-size:20px;display:block}
.entity{page-break-inside:avoid}
pre{background:#f6f8fa;border:1px solid var(--line);padding:10px;overflow:auto;font-size:11px}
@page{size:A4;margin:16mm 14mm}
@media print{main{padding:0;max-width:none} h2{page-break-after:avoid} .entity,tr{page-break-inside:avoid}
 .noprint{display:none}}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _expected(case: dict) -> str:
    exp = case.get("expect") or {}
    if case["kind"] == "column":
        if exp.get("detected"):
            out = f"detected as <b>{_e(exp.get('entity_type') or 'PII')}</b>"
            if exp.get("rule"):
                out += f"<br><span class='small muted'>edge rule <code>{_e(exp['rule'])}</code></span>"
            return out
        if exp.get("evidence_entity"):
            return f"<span class='chip warn'>evidence only</span> {_e(exp['evidence_entity'])} candidate"
        return "not detected"
    parts = [f"<b>{_e(s['entity_type'])}</b>: <span dir='auto'>{_e(s['value'])}</span>"
             for s in exp.get("spans") or []]
    parts += [f"not reported: <span dir='auto'>{_e(a['value'])}</span>" for a in exp.get("absent") or []]
    return "<br>".join(parts) or "—"


def _example(case: dict) -> str:
    if case["kind"] == "column":
        cols = [(case["column"], case["values"])] + list((case.get("with") or {}).items())
        return "<br>".join(
            f"column <code dir='auto'>{_e(name)}</code>: <span class='mono vals' dir='auto'>"
            f"{_e(', '.join(str(v) for v in values))}</span>" for name, values in cols)
    lang = case.get("language", "en")
    return f"text ({_e(lang)}): <span dir='auto'>“{_e(case['text'])}”</span>"


def _actual(case: dict, res: Optional[dict]) -> str:
    if not res or not res.get("actual"):
        return ""
    a = res["actual"]
    if case["kind"] == "column":
        if a["detected"]:
            return f"{_e(a['entity_type'])}" + (f" · rule {_e(a['rule'])}" if a.get("rule") else "")
        return "not detected" + (f" · evidence {_e(a['evidence_entity'])}" if a.get("evidence_entity") else "")
    spans = a.get("spans") or []
    return "; ".join(f"{_e(s['entity_type'])}: <span dir='auto'>{_e(s['value'])}</span>" for s in spans) or "no spans"


def _status(res: Optional[dict]) -> str:
    label, cls = _STATUS.get((res or {}).get("status"), _STATUS[None])
    title = "; ".join((res or {}).get("problems") or [])
    return f"<span class='chip {cls}' title='{_e(title)}'>{label}</span>"


def _cases_table(cases: list[dict], results: dict[str, dict]) -> str:
    rows = []
    for c in cases:
        res = results.get(c["id"])
        note = f"<div class='small muted'>{_e(c['note'])}</div>" if c.get("note") else ""
        req = (f"<div class='small muted'>needs: {_e(', '.join(c['requires']))}</div>"
               if c.get("requires") else "")
        rows.append(
            f"<tr><td>{_e(c['title'])}{note}{req}<div class='small muted mono'>{_e(c['id'])}</div></td>"
            f"<td>{_example(c)}</td><td>{_expected(c)}</td>"
            f"<td>{_status(res)}<div class='small muted'>{_actual(c, res)}</div></td></tr>")
    return ("<table><thead><tr><th style='width:22%'>Case</th><th>Example</th>"
            "<th style='width:20%'>Expected</th><th style='width:18%'>Engine result</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _catalog_details(entity: str) -> str:
    """How the entity is detected, read live from the regex catalogue."""
    try:
        from redibis.pii.regex_catalog import list_catalog

        entries = [e for e in list_catalog() if e["entity_type"] == entity
                   or (entity == "JWT" and e["entity_type"] == "JWT_TOKEN")]
    except Exception:  # noqa: BLE001
        entries = []
    if not entries:
        return ""
    groups = sorted({"CSV columns" if e["recognizer_group"] == "structured" else "free text"
                     for e in entries})
    validators = sorted({e["requires_validator"] for e in entries if e.get("requires_validator")})
    hints = sorted({h for e in entries for h in (e.get("context_hints") or ())})
    return ("<table><tbody>"
            f"<tr><th style='width:22%'>Patterns</th><td>{len(entries)} "
            f"({_e(', '.join(groups))}): <span class='mono small'>{_e(', '.join(e['name'] for e in entries))}</span></td></tr>"
            f"<tr><th>Validators</th><td class='mono small'>{_e(', '.join(validators) or 'none (shape only)')}</td></tr>"
            f"<tr><th>Column-name context</th><td class='small' dir='auto'>{_e(', '.join(hints[:40]))}"
            f"{' …' if len(hints) > 40 else ''}</td></tr></tbody></table>")


def _edge_rule_text(pack: str) -> dict[str, dict]:
    try:
        from redibis.classification.pack_store import load_pack

        policy = load_pack(pack)
        rules = getattr(policy, "edge_rules", None) or []
        out = {}
        for r in rules:
            d = r if isinstance(r, dict) else getattr(r, "__dict__", {})
            rid = d.get("id") or getattr(r, "id", None)
            if rid:
                out[rid] = {"when": d.get("when") or getattr(r, "when", None),
                            "then": d.get("then") or getattr(r, "then", None)}
        return out
    except Exception:  # noqa: BLE001
        return {}


def render_contract_html(contract: dict, verification: Optional[dict] = None) -> str:
    results = {r["id"]: r for r in (verification or {}).get("results", [])}
    summary = (verification or {}).get("summary") or {}
    env = (verification or {}).get("environment") or {}
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entities = contract.get("entities", [])

    def detected_in(section: dict, kind: str) -> str:
        cases = [c for c in section["cases"] if c["kind"] == kind]
        if not cases:
            return "<span class='muted'>—</span>"
        good = [c for c in cases if (c["kind"] == "text" and c["expect"].get("spans"))
                or (c["kind"] == "column" and c["expect"].get("detected"))]
        if not good:
            return "<span class='chip warn'>evidence only</span>"
        needs = all(c.get("requires") for c in good)
        return "<span class='chip muted'>with NER</span>" if needs else "<span class='chip ok'>yes</span>"

    def section_status(section: dict) -> str:
        sts = [(results.get(c["id"]) or {}).get("status") for c in section["cases"]]
        if not verification:
            return _status(None)
        if "failed" in sts:
            return _status({"status": "failed"})
        return _status({"status": "passed"}) if "passed" in sts else _status({"status": "not_verified"})

    overview = "".join(
        f"<tr><td><a href='#{_e(s['entity_type'])}'>{_e(s['title'])}</a><div class='small mono muted'>{_e(s['entity_type'])}</div></td>"
        f"<td>{_e(s['category'])}</td><td>{detected_in(s, 'column')}</td><td>{detected_in(s, 'text')}</td>"
        f"<td style='text-align:right'>{len(s['cases'])}</td><td>{section_status(s)}</td></tr>"
        for s in entities)

    engines_env = env.get("engines") or {}
    env_line = ", ".join(f"{k}: {'on' if v else 'off'}" for k, v in engines_env.items())
    kpis = "".join(f"<div class='kpi'><b>{summary.get(k, '—')}</b>{label}</div>" for k, label in
                   (("total", "examples"), ("passed", "verified"), ("failed", "do not hold"),
                    ("not_verified", "not verified (optional model)")))
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_e(contract.get('title'))} {_e(contract.get('version'))}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{_e(contract.get('title'))}</h1>",
        f"<div class='muted'>Version {_e(contract.get('version'))} · generated {generated}"
        + (f" · redibis {_e(env.get('redibis_version'))}" if env.get("redibis_version") else "") + "</div>",
        "<div class='cover'>",
        f"<p>{_e(contract.get('summary'))}</p>",
        f"<p class='small'><b>Parties:</b> {_e(contract['parties']['technology'])} · {_e(contract['parties']['legal'])}</p>",
        (f"<div class='kpis'>{kpis}</div><p class='small muted'>Verified {_e(verification.get('verified_at'))} "
         f"in {_e(verification.get('seconds'))} s · policy pack <code>{_e(env.get('policy_pack'))}</code> · "
         f"engines — {_e(env_line)}</p>") if verification else
        "<p class='chip warn'>Not verified — run the verification before sharing this document.</p>",
        "</div>",
        "<h2>1. How to read this document</h2><table><tbody>",
        "".join(f"<tr><th style='width:22%'>{_e(k.replace('_', ' '))}</th><td>{_e(v)}</td></tr>"
                for k, v in contract.get("outcomes", {}).items()),
        "</tbody></table>",
        "<h3>Engines</h3><table><tbody>",
        "".join(f"<tr><th style='width:22%'>{_e(x['name'])}</th><td>{_e(x['role'])}</td></tr>"
                for x in contract.get("engines", [])),
        "</tbody></table>",
        "<h2>2. PII types at a glance</h2>",
        "<table><thead><tr><th>PII type</th><th>Category</th><th>CSV columns</th><th>Free text</th>"
        f"<th>Examples</th><th>Status</th></tr></thead><tbody>{overview}</tbody></table>",
        "<h2>3. PII types in detail</h2>",
    ]
    for s in entities:
        parts += [f"<div class='entity' id='{_e(s['entity_type'])}'><h3>{_e(s['title'])} "
                  f"<span class='small mono muted'>{_e(s['entity_type'])}</span></h3>",
                  f"<p>{_e(s['description'])}</p>", _catalog_details(s["entity_type"]),
                  _cases_table(s["cases"], results), "</div>"]
    n = 4
    for key, heading in (("context", "Column names and context"), ("text_gateway", "Text Gateway"),
                         ("edge_rules", "Edge rules"), ("not_supported", "What is not detected")):
        for sec in contract.get(key, []) or []:
            parts += [f"<h2>{n}. {_e(sec.get('title') or heading)}</h2>", f"<p>{_e(sec.get('description'))}</p>"]
            if key == "edge_rules":
                texts = _edge_rule_text(sec.get("pack", "telecom"))
                used = [c["expect"].get("rule") for c in sec["cases"] if c["expect"].get("rule")]
                if texts:
                    parts.append("<table><thead><tr><th>Rule</th><th>When</th><th>Then</th></tr></thead><tbody>"
                                 + "".join(f"<tr><td class='mono'>{_e(r)}</td><td class='mono small'>{_e(json.dumps(texts.get(r, {}).get('when'), ensure_ascii=False))}</td>"
                                           f"<td class='mono small'>{_e(json.dumps(texts.get(r, {}).get('then'), ensure_ascii=False))}</td></tr>"
                                           for r in dict.fromkeys(used)) + "</tbody></table>")
            parts.append(_cases_table(sec["cases"], results))
            n += 1
    proc = contract.get("change_process") or {}
    parts += [f"<h2>{n}. Adding or changing a PII type</h2>", f"<p>{_e(proc.get('summary'))}</p>", "<ol>",
              "".join(f"<li>{_e(step)}</li>" for step in proc.get("steps", [])), "</ol>",
              "<h3>Request template</h3>",
              f"<pre>{_e(json.dumps(proc.get('request_template', {}), indent=2, ensure_ascii=False))}</pre>",
              "<p class='small muted'>Generated by redibis — every example above is run through the engines; "
              "the document never states a capability the engine was not shown to have.</p>",
              "</main></body></html>"]
    return "".join(parts)


def markdown_page(text: str, *, title: str) -> str:
    """Markdown → the same printable page style as the contract."""
    try:
        from markdown_it import MarkdownIt

        body = MarkdownIt("commonmark").enable("table").render(text)
    except Exception:  # noqa: BLE001 — markdown-it is optional; fall back to plain text
        body = f"<pre>{_e(text)}</pre>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>{body}</main></body></html>")


# ── PDF ──────────────────────────────────────────────────────────────────────

def find_chromium(explicit: Optional[str] = None) -> Optional[str]:
    candidates = [explicit, os.environ.get("REDIBIS_CHROMIUM")]
    candidates += [shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome",
                                             "google-chrome-stable", "chrome")]
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "", str(Path.home() / ".cache/ms-playwright"),
             "/opt/pw-browsers"]
    for root in filter(None, roots):
        candidates += sorted(glob.glob(os.path.join(root, "chromium-*/chrome-linux/chrome")), reverse=True)
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


def html_to_pdf(html_text: str, *, chromium: Optional[str] = None, timeout: int = 120) -> bytes:
    binary = find_chromium(chromium)
    if not binary:
        raise RuntimeError("PDF needs Chromium: install it, or set REDIBIS_CHROMIUM to its path "
                           "(or download the HTML and print it to PDF from a browser)")
    with tempfile.TemporaryDirectory(prefix="redibis_pdf_") as tmp:
        src, out = Path(tmp) / "doc.html", Path(tmp) / "doc.pdf"
        src.write_text(html_text, encoding="utf-8")
        cmd = [binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
               f"--user-data-dir={tmp}/profile", f"--print-to-pdf={out}", src.as_uri()]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError(f"Chromium did not produce a PDF: {proc.stderr.strip()[-400:]}")
        return out.read_bytes()
