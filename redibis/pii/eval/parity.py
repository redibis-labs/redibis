"""Fingerprint eval reports so independent-mode results can be golden-diffed.

Usage:
    python -m redibis.pii.eval.parity BASELINE.json CURRENT.json
    python -m redibis.pii.eval.parity --write GOLDEN.json REPORT.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

KIND = "redibis.text_eval_parity"


def _span_key(span: Mapping[str, Any]) -> tuple:
    return (
        int(span.get("start") or 0),
        int(span.get("end") or 0),
        str(span.get("entity_type") or ""),
        str(span.get("engine") or ""),
        round(float(span.get("score") or 0.0), 4),
    )


def _iter_cases(report: Mapping[str, Any]):
    kind = str(report.get("kind") or "")
    if kind.endswith("batch_report") or report.get("files"):
        for entry in report.get("files") or []:
            prefix = str(entry.get("dataset_id") or entry.get("path") or "")
            for case in entry.get("cases") or []:
                cid = str(case.get("id") or "")
                yield f"{prefix}/{cid}" if prefix else cid, case
        return
    for case in report.get("cases") or []:
        yield str(case.get("id") or ""), case


def fingerprint_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Stable predicted-span identity, ignoring timestamps and provenance."""
    cases: dict[str, list[dict[str, Any]]] = {}
    for case_id, case in _iter_cases(report):
        spans = []
        for span in case.get("predicted_spans") or []:
            start, end, et, engine, score = _span_key(span)
            spans.append({
                "start": start,
                "end": end,
                "entity_type": et,
                "engine": engine,
                "score": score,
            })
        spans.sort(key=lambda s: (s["start"], s["end"], s["entity_type"], s["engine"]))
        cases[case_id] = spans
    return {"kind": KIND, "cases": dict(sorted(cases.items()))}


def load_fingerprint(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if str(data.get("kind") or "") == KIND:
        return data
    return fingerprint_report(data)


def diff_fingerprints(expected: Mapping[str, Any], got: Mapping[str, Any]) -> list[str]:
    exp_cases = dict(expected.get("cases") or {})
    got_cases = dict(got.get("cases") or {})
    diffs: list[str] = []
    for key in sorted(set(exp_cases) | set(got_cases)):
        a = exp_cases.get(key)
        b = got_cases.get(key)
        if a is None:
            diffs.append(f"+ {key}: {b}")
        elif b is None:
            diffs.append(f"- {key}: {a}")
        elif a != b:
            diffs.append(f"~ {key}: {a} -> {b}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    write = False
    if args and args[0] == "--write":
        write = True
        args = args[1:]
    if len(args) != 2:
        print(
            "usage: python -m redibis.pii.eval.parity [--write] BASELINE.json CURRENT.json",
            file=sys.stderr,
        )
        return 2
    baseline_path = Path(args[0])
    current_path = Path(args[1])
    if not current_path.is_file():
        print(f"eval-parity: missing current report {current_path}", file=sys.stderr)
        return 2
    current = fingerprint_report(json.loads(current_path.read_text(encoding="utf-8")))
    if write:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        print(f"eval-parity: wrote {baseline_path}")
        return 0
    if not baseline_path.is_file():
        print(f"eval-parity: missing baseline {baseline_path}", file=sys.stderr)
        return 2
    expected = load_fingerprint(baseline_path)
    diffs = diff_fingerprints(expected, current)
    if diffs:
        print(f"eval-parity: {len(diffs)} difference(s) vs {baseline_path}", file=sys.stderr)
        for row in diffs[:50]:
            print(row, file=sys.stderr)
        if len(diffs) > 50:
            print(f"... {len(diffs) - 50} more", file=sys.stderr)
        return 1
    print(f"eval-parity: ok ({len(current.get('cases') or {})} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
