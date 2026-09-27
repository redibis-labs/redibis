"""PII capability contract: what this installation detects, proven by examples.

One versioned JSON file (``contract.json`` next to this module, or any file with
the same shape) lists every PII type the engines detect — in CSV cell values, in
column names and context, through edge rules, and in free text via the Text
Gateway — each with worked examples, plus what is **not** detected, also with
examples. It is the agreement between the technology and privacy/legal teams:

    verify()        run every example through the real engines (no stubs)
    render_html()   the formal document, with each example's verified status
    render_pdf()    the same document as PDF (headless Chromium)

``redibis pii capabilities verify|report``, ``/api/pii/capabilities/*`` and the
Reports page use these functions; ``tests/test_pii_capability_contract.py`` runs
``verify()`` on every change, so the document cannot claim what the engine
does not do.

Case kinds (``case["kind"]``):

``column``  a CSV column — ``column`` name and ``values`` (plus optional companion
            columns in ``with``, e.g. a longitude next to a latitude) — through the
            table scan (detector → equation → edge rules). ``expect``: ``detected``
            (bool), optional ``entity_type``, optional ``rule`` (edge-rule id that
            decided), optional ``evidence_entity`` (what the patterns matched when the
            column is *not* confirmed — "evidence only").
``text``    a text through the Text Gateway. ``expect``: ``spans`` that must be
            found (``entity_type`` + ``value``) and ``absent`` values that must not.

``requires`` (``["ner"]`` / ``["llm"]``) marks cases that need an optional model;
they are reported as *not verified* where that model is not installed.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "CONTRACT_KIND",
    "DEFAULT_CONTRACT",
    "environment",
    "iter_cases",
    "guide_markdown",
    "load_contract",
    "render_guide_html",
    "render_html",
    "render_pdf",
    "verify",
    "verify_case",
]

CONTRACT_KIND = "redibis.pii_capability_contract"
RESULTS_KIND = "redibis.pii_capability_verification"
DEFAULT_CONTRACT = Path(__file__).with_name("contract.json")


def load_contract(path: Optional[str | Path] = None) -> dict:
    """The contract JSON (the shipped one by default); checks its ``kind``."""
    data = json.loads(Path(path or DEFAULT_CONTRACT).read_text(encoding="utf-8"))
    if data.get("kind") != CONTRACT_KIND:
        raise ValueError(f"not a PII capability contract (kind={data.get('kind')!r})")
    return data


def iter_cases(contract: dict) -> Iterable[tuple[dict, dict]]:
    """``(section, case)`` for every case: entity sections, context, text gateway, edge rules, limits."""
    for section in contract.get("entities", []) or []:
        for case in section.get("cases", []) or []:
            yield section, case
    for key in ("context", "text_gateway", "edge_rules", "not_supported"):
        for section in contract.get(key, []) or []:
            for case in section.get("cases", []) or []:
                yield section, case


# ── environment ──────────────────────────────────────────────────────────────

def environment(redibis_config: Any = None) -> dict:
    """Which engines this installation can run (NER / LLM are optional)."""
    from redibis import __version__ as redibis_version
    from redibis.config import RedibisConfig

    cfg = redibis_config or RedibisConfig.load()
    ner = False
    try:
        from redibis.pii.ner_registry import NERModelRegistry

        ner = NERModelRegistry.try_load_pii(cfg.pii) is not None
    except Exception:  # noqa: BLE001 — no model configured / optional deps missing
        ner = False
    llm = bool(getattr(getattr(cfg.pii, "llm", None), "enabled", False))
    return {
        "redibis_version": redibis_version,
        "policy_pack": getattr(cfg.classification, "policy_pack", "telecom"),
        "edge_rules_enabled": bool(getattr(cfg.classification, "edge_rules_enabled", True)),
        "engines": {"regex": True, "phone": True, "edge_rules": True, "ner": ner, "llm": llm},
    }


# ── verification ─────────────────────────────────────────────────────────────

def _column_actual(case: dict, env: dict) -> dict:
    import pandas as pd

    from redibis.services.pipeline import run_pii_detection

    frame = {case["column"]: list(case["values"])}
    frame.update({k: list(v) for k, v in (case.get("with") or {}).items()})
    df = pd.DataFrame(frame)
    engines = "both" if env["engines"]["ner"] else "regex"
    detections = run_pii_detection(df, engines=engines, policy_pack=env["policy_pack"],
                                   edge_rules_enabled=env["edge_rules_enabled"])
    det = next(d for d in detections if d.column == case["column"])
    path = det.decision_path or ""
    rule = path.split("edge_rule:", 1)[1].split()[0] if "edge_rule:" in path else None
    return {"detected": bool(det.detected), "entity_type": det.entity_type if det.detected else None,
            "evidence_entity": det.entity_type, "confidence": round(float(det.confidence or 0), 3),
            "rule": rule, "pattern": det.presidio_pattern,
            "match_rate": det.presidio_match_rate}


def _text_actual(case: dict, env: dict, service: Any) -> dict:
    result = service.scan(case["text"], language=case.get("language", "en"),
                          engines="both" if env["engines"]["ner"] else "regex")
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    spans = [{"entity_type": s.get("entity_type"), "value": s.get("text") or s.get("value"),
              "start": s.get("start"), "end": s.get("end"), "score": s.get("score"),
              "source": s.get("source")} for s in data.get("spans") or []]
    return {"spans": spans}


def _covers(span_value: str, want: str) -> bool:
    return bool(span_value) and (want in span_value or span_value in want)


def _judge(case: dict, actual: dict) -> list[str]:
    """Differences between the expectation and what the engine did (empty = pass)."""
    exp = case.get("expect") or {}
    problems: list[str] = []
    if case["kind"] == "column":
        if bool(exp.get("detected")) != actual["detected"]:
            problems.append(f"detected={actual['detected']} (expected {bool(exp.get('detected'))})")
        if exp.get("detected") and exp.get("entity_type") and actual["entity_type"] != exp["entity_type"]:
            problems.append(f"entity {actual['entity_type']} (expected {exp['entity_type']})")
        if exp.get("evidence_entity") and actual["evidence_entity"] != exp["evidence_entity"]:
            problems.append(f"evidence {actual['evidence_entity']} (expected {exp['evidence_entity']})")
        if exp.get("rule") and actual["rule"] != exp["rule"]:
            problems.append(f"decided by {actual['rule'] or 'the engines'} (expected edge rule {exp['rule']})")
        return problems
    spans = actual["spans"]
    for want in exp.get("spans", []) or []:
        if not any(s["entity_type"] == want["entity_type"] and _covers(s["value"] or "", want["value"])
                   for s in spans):
            problems.append(f"missing {want['entity_type']} {want['value']!r}")
    for bad in exp.get("absent", []) or []:
        hits = [s for s in spans if _covers(s["value"] or "", bad["value"])
                and (not bad.get("entity_type") or s["entity_type"] == bad["entity_type"])]
        if hits:
            problems.append(f"{bad['value']!r} was detected as {hits[0]['entity_type']}")
    return problems


def verify_case(case: dict, env: Optional[dict] = None, *, service: Any = None) -> dict:
    """Run one case; returns ``{id, status: passed|failed|not_verified, actual, problems}``."""
    env = env or environment()
    missing = [r for r in case.get("requires", []) or [] if not env["engines"].get(r)]
    if missing:
        return {"id": case["id"], "status": "not_verified", "actual": None,
                "problems": [f"needs {', '.join(missing)} (not installed here)"]}
    try:
        if case["kind"] == "column":
            actual = _column_actual(case, env)
        elif case["kind"] == "text":
            if service is None:
                from redibis.services.text_pii_service import TextPIIService

                service = TextPIIService()
            actual = _text_actual(case, env, service)
        else:
            raise ValueError(f"unknown case kind {case['kind']!r}")
    except Exception as exc:  # noqa: BLE001 — reported, never hidden
        return {"id": case["id"], "status": "failed", "actual": None,
                "problems": [f"error: {type(exc).__name__}: {exc}"]}
    problems = _judge(case, actual)
    return {"id": case["id"], "status": "failed" if problems else "passed",
            "actual": actual, "problems": problems}


def verify(contract: Optional[dict] = None, *, only: Optional[Iterable[str]] = None,
           progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Run every case of ``contract`` (the shipped one by default) through the engines."""
    from redibis.services.text_pii_service import TextPIIService

    contract = contract or load_contract()
    env = environment()
    service = TextPIIService()
    wanted = set(only or [])
    cases = [c for _s, c in iter_cases(contract) if not wanted or c["id"] in wanted]
    started = time.perf_counter()
    results = []
    for i, case in enumerate(cases, 1):
        results.append(verify_case(case, env, service=service))
        if progress:
            progress(i, len(cases), case["id"])
    counts = {k: sum(1 for r in results if r["status"] == k)
              for k in ("passed", "failed", "not_verified")}
    return {
        "kind": RESULTS_KIND,
        "contract_version": contract.get("version"),
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.perf_counter() - started, 2),
        "environment": env,
        "summary": {"total": len(results), **counts},
        "results": results,
    }


# ── rendering ────────────────────────────────────────────────────────────────

def render_html(contract: Optional[dict] = None, verification: Optional[dict] = None) -> str:
    """The formal document (self-contained HTML, prints on A4)."""
    from redibis.pii.capability.render import render_contract_html

    return render_contract_html(contract or load_contract(), verification)


def render_pdf(html: str, *, chromium: Optional[str] = None, timeout: int = 120) -> bytes:
    """``html`` as PDF through headless Chromium (Arabic shaping included).

    Chromium comes from ``chromium`` (argument), ``REDIBIS_CHROMIUM`` or the usual
    names on ``PATH``. Raises ``RuntimeError`` when none is found.
    """
    from redibis.pii.capability.render import html_to_pdf

    return html_to_pdf(html, chromium=chromium, timeout=timeout)


GUIDE = Path(__file__).with_name("tuning_guide.md")


def guide_markdown() -> str:
    """The "adding and tuning PII detection" guide (Markdown)."""
    return GUIDE.read_text(encoding="utf-8")


def render_guide_html() -> str:
    """The tuning guide as a self-contained, printable HTML page."""
    from redibis.pii.capability.render import markdown_page

    return markdown_page(guide_markdown(), title="Adding and tuning PII detection")
