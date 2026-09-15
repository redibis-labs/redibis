"""Batch free-text PII scanning — directory / JSONL / CSV → per-document results.

One ``TextPIIService`` for the whole run so NER loads once. Failures are
isolated per document. The run registry stores digests only (never raw text).
"""

from __future__ import annotations

import csv
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse

logger = logging.getLogger("pii.text_batch")

_ID_SAFE = re.compile(r"[^\w.\-]")
LLM_CIRCUIT_THRESHOLD = 5
LLM_RETRY_ATTEMPTS = 3
LLM_RETRY_BASE_S = 0.4

ProgressCb = Callable[[str, dict[str, Any]], None]


def sanitize_doc_id(raw: str) -> str:
    """Filename-safe id. Same spirit as ``_sanitize_sample_session_id``, but
    dots are kept so ``note.txt`` stays distinct from ``notetxt``.
    """
    safe = _ID_SAFE.sub("", (raw or "").strip()).replace("..", "")
    return safe[:64]


def discover_text_files(
    root: Path,
    *,
    glob_pat: str = "*.txt",
    recursive: bool = False,
) -> list[Path]:
    """Sorted regular files under ``root``. Symlinks and escapes are skipped."""
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    pattern = f"**/{glob_pat}" if recursive else glob_pat
    resolved = root.resolve()
    return sorted(
        p
        for p in root.glob(pattern)
        if p.is_file()
        and not p.is_symlink()
        and p.resolve().is_relative_to(resolved)
    )


@dataclass
class BatchDocument:
    doc_id: str
    source: str
    text: str
    sanitized_id: str = ""
    load_error: str = ""

    def __post_init__(self) -> None:
        if not self.sanitized_id:
            self.sanitized_id = sanitize_doc_id(self.doc_id) or f"row-{abs(hash(self.source)) % 10**8}"


def _unique_or_abort(docs: Sequence[BatchDocument]) -> None:
    seen: dict[str, str] = {}
    for doc in docs:
        key = doc.sanitized_id
        if key in seen:
            raise ValueError(
                f"duplicate document id {doc.doc_id!r} (sanitized {key!r}): "
                f"{seen[key]!r} and {doc.source!r}"
            )
        seen[key] = doc.source


def documents_from_directory(
    path: Path,
    *,
    glob_pat: str = "*.txt",
    recursive: bool = False,
) -> list[BatchDocument]:
    root = path.resolve()
    docs: list[BatchDocument] = []
    for file_path in discover_text_files(path, glob_pat=glob_pat, recursive=recursive):
        rel = file_path.resolve().relative_to(root).as_posix()
        try:
            text = file_path.read_text(encoding="utf-8")
            load_error = ""
        except (UnicodeDecodeError, OSError) as exc:
            text = ""
            load_error = str(exc)
        docs.append(BatchDocument(
            doc_id=rel, source=str(file_path), text=text, load_error=load_error,
        ))
    _unique_or_abort(docs)
    return docs


def documents_from_list(manifest: Path) -> list[BatchDocument]:
    lines = manifest.read_text(encoding="utf-8").splitlines() if str(manifest) != "-" else []
    docs: list[BatchDocument] = []
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        p = Path(raw)
        if not p.is_file() or p.is_symlink():
            raise FileNotFoundError(f"input-list entry is not a regular file: {raw}")
        rel = p.name
        docs.append(BatchDocument(
            doc_id=rel, source=str(p), text=p.read_text(encoding="utf-8"),
        ))
    _unique_or_abort(docs)
    return docs


def documents_from_jsonl(
    path: Path | str,
    *,
    text_field: str = "body",
    id_field: str = "",
    stream: Optional[Iterable[str]] = None,
) -> list[BatchDocument]:
    if stream is None:
        raw = Path(path).read_text(encoding="utf-8") if str(path) != "-" else ""
        lines = raw.splitlines()
    else:
        lines = list(stream)
    docs: list[BatchDocument] = []
    for i, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            docs.append(BatchDocument(
                doc_id=f"row-{i}", source=f"{path}:{i}", text="",
                load_error=f"invalid JSON: {exc}",
            ))
            continue
        if not isinstance(row, dict):
            continue
        text = str(row.get(text_field) or "")
        doc_id = str(row.get(id_field) or "").strip() if id_field else ""
        if not doc_id:
            doc_id = f"row-{i}"
        docs.append(BatchDocument(doc_id=doc_id, source=f"{path}:{i}", text=text))
    _unique_or_abort(docs)
    return docs


def documents_from_csv(
    path: Path,
    *,
    text_column: str = "note",
    id_column: str = "",
) -> list[BatchDocument]:
    docs: list[BatchDocument] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=1):
            text = str((row or {}).get(text_column) or "")
            doc_id = str((row or {}).get(id_column) or "").strip() if id_column else ""
            if not doc_id:
                doc_id = f"row-{i}"
            docs.append(BatchDocument(doc_id=doc_id, source=f"{path}:{i}", text=text))
    _unique_or_abort(docs)
    return docs


@dataclass
class BatchRunConfig:
    language: str = "en"
    engines: str = "both"
    min_score: float = 0.35
    resolve: str = "priority"
    use_llm: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_endpoint: str = ""
    equation: str = "independent"
    entities: tuple[str, ...] = ()
    return_text: bool = True
    include_text_in_report: bool = False
    include_arbitration: bool = False
    max_chars: int = 50_000
    workers: int = 1
    continue_on_error: bool = True
    resume: bool = False
    limit: int = 0
    require_llm: bool = False
    require_ner: bool = False
    deidentify: bool = False
    policy_id: str = ""
    quiet: bool = False
    llm_circuit_threshold: int = LLM_CIRCUIT_THRESHOLD


@dataclass
class BatchRunResult:
    documents: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    aggregates: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, str] = field(default_factory=dict)
    exit_code: int = 0


def _transportish(reason: str) -> bool:
    lowered = (reason or "").lower()
    needles = (
        "connection", "timeout", "timed out", "temporarily unavailable",
        "connection refused", "reset by peer", "connect error", "httpx",
        "status 5", "502", "503", "504",
    )
    return any(n in lowered for n in needles)


def _scan_one(svc, doc: BatchDocument, cfg: BatchRunConfig, *, use_llm: bool):
    from redibis.services.text_pii_service import TextPIIServiceError

    kwargs = dict(
        language=cfg.language,
        engines=cfg.engines,
        min_score=cfg.min_score,
        return_text=cfg.return_text,
        resolve=cfg.resolve,
        use_llm=use_llm,
        entities=list(cfg.entities),
        max_chars=cfg.max_chars,
        llm_provider=cfg.llm_provider,
        llm_model=cfg.llm_model,
        llm_api_key=cfg.llm_api_key,
        llm_endpoint=cfg.llm_endpoint,
        equation=cfg.equation,
        include_arbitration=cfg.include_arbitration,
    )
    last_exc: Optional[BaseException] = None
    for attempt in range(LLM_RETRY_ATTEMPTS if use_llm else 1):
        try:
            return svc.scan(doc.text, **kwargs)
        except TextPIIServiceError:
            raise
        except Exception as exc:
            last_exc = exc
            if use_llm and _transportish(str(exc)) and attempt + 1 < LLM_RETRY_ATTEMPTS:
                time.sleep(LLM_RETRY_BASE_S * (2 ** attempt))
                continue
            raise
    raise last_exc or RuntimeError("scan failed")


def _completed_ids(out_dir: Path) -> set[str]:
    docs = out_dir / "documents"
    if not docs.is_dir():
        return set()
    return {p.stem for p in docs.glob("*.json") if p.is_file()}


def run_text_batch(
    svc,
    documents: Sequence[BatchDocument],
    cfg: BatchRunConfig,
    *,
    out_dir: Optional[Path] = None,
    progress_cb: Optional[ProgressCb] = None,
) -> BatchRunResult:
    """Scan ``documents`` with one service. Writes per-document JSON when ``out_dir`` set."""

    result = BatchRunResult()
    pending = list(documents)
    if cfg.limit and cfg.limit > 0:
        pending = pending[: cfg.limit]

    already: set[str] = set()
    if cfg.resume and out_dir is not None:
        already = _completed_ids(out_dir)
        kept = []
        for doc in pending:
            if doc.sanitized_id in already:
                result.skipped.append({
                    "id": doc.doc_id, "sanitized_id": doc.sanitized_id,
                    "status": "skipped", "reason": "already present in out-dir",
                })
            else:
                kept.append(doc)
        pending = kept

    for doc in pending:
        result.manifest[doc.sanitized_id] = doc.source

    workers = max(1, int(cfg.workers or 1))
    circuit_open = False
    consecutive_llm_fail = 0
    findings_total = 0
    contested_total = 0
    entity_counts: dict[str, int] = {}
    score_buckets: dict[str, list[float]] = {}
    engines_ran: set[str] = set()
    engines_unavailable: dict[str, str] = {}
    arb_totals = {
        "contested": 0, "confirmed": 0, "vetoed": 0,
        "type_conflicts": 0, "boundary_conflicts": 0, "llm_only": 0,
    }
    coverage_block: dict[str, Any] = {}
    llm_unavailable_run = False

    if out_dir is not None:
        (out_dir / "documents").mkdir(parents=True, exist_ok=True)
        if cfg.deidentify:
            (out_dir / "redacted").mkdir(parents=True, exist_ok=True)

    def _handle(doc: BatchDocument, use_llm: bool) -> dict[str, Any]:
        if doc.load_error:
            return {
                "id": doc.doc_id,
                "sanitized_id": doc.sanitized_id,
                "status": "error",
                "error": doc.load_error,
                "source": doc.source,
            }
        try:
            scan = _scan_one(svc, doc, cfg, use_llm=use_llm)
        except Exception as exc:
            return {
                "id": doc.doc_id,
                "sanitized_id": doc.sanitized_id,
                "status": "error",
                "error": str(exc),
                "source": doc.source,
            }
        payload = scan.to_dict(return_text=cfg.return_text)
        payload["id"] = doc.doc_id
        payload["sanitized_id"] = doc.sanitized_id
        payload["status"] = "ok"
        payload["source"] = doc.source
        if not cfg.return_text:
            for span in payload.get("spans") or []:
                span.pop("text", None)
                span["text"] = ""
        return {"_result": scan, "_payload": payload, "doc": doc}

    def _after(entry: dict[str, Any], *, force_llm_unavailable: str = "") -> None:
        nonlocal findings_total, contested_total, consecutive_llm_fail
        nonlocal circuit_open, llm_unavailable_run
        if entry.get("status") == "error":
            result.errors.append({
                "id": entry.get("id"),
                "status": "error",
                "error": entry.get("error"),
            })
            result.documents.append({
                "id": entry.get("id"),
                "sanitized_id": entry.get("sanitized_id"),
                "status": "error",
                "error": entry.get("error"),
            })
            return
        scan = entry["_result"]
        payload = dict(entry["_payload"])
        if force_llm_unavailable:
            unavailable = dict(payload.get("engines_unavailable") or {})
            unavailable["llm"] = force_llm_unavailable
            payload["engines_unavailable"] = unavailable
            llm_unavailable_run = True
        doc: BatchDocument = entry["doc"]
        if out_dir is not None:
            dest = out_dir / "documents" / f"{doc.sanitized_id}.json"
            dest.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if cfg.deidentify:
                _write_redacted(svc, doc, scan, cfg, out_dir)
        result.documents.append({
            "id": doc.doc_id,
            "sanitized_id": doc.sanitized_id,
            "status": "ok",
            "char_count": payload.get("char_count"),
            "entity_counts": payload.get("entity_counts") or {},
            "engines_ran": payload.get("engines_ran") or [],
            "arbitration": (payload.get("arbitration") or {}) if cfg.equation != "independent" else {},
        })
        for et, n in dict(payload.get("entity_counts") or {}).items():
            entity_counts[et] = entity_counts.get(et, 0) + int(n)
        for span in payload.get("spans") or []:
            findings_total += 1
            et = str(span.get("entity_type") or "")
            score_buckets.setdefault(et, []).append(float(span.get("score") or 0))
            if span.get("agreement") in ("type_conflict", "boundary_conflict", "vetoed"):
                contested_total += 1
        for name in payload.get("engines_ran") or []:
            engines_ran.add(str(name))
        for k, v in dict(payload.get("engines_unavailable") or {}).items():
            engines_unavailable.setdefault(str(k), str(v))
        arb = dict(payload.get("arbitration") or {})
        for key in arb_totals:
            arb_totals[key] += int(arb.get(key) or 0)
        cov = dict(payload.get("coverage") or {})
        if cov:
            coverage_block.setdefault("documents_with_coverage", 0)
            coverage_block["documents_with_coverage"] += 1
            if cov.get("scanned_fraction") is not None:
                coverage_block.setdefault("scanned_fraction_sum", 0.0)
                coverage_block["scanned_fraction_sum"] += float(cov.get("scanned_fraction") or 0)
        llm_miss = "llm" in dict(payload.get("engines_unavailable") or {}) or (
            cfg.use_llm and "llm" not in (payload.get("engines_ran") or [])
        )
        if cfg.use_llm and llm_miss and not force_llm_unavailable:
            consecutive_llm_fail += 1
            llm_unavailable_run = True
            if consecutive_llm_fail >= cfg.llm_circuit_threshold:
                circuit_open = True
        elif cfg.use_llm and not llm_miss:
            consecutive_llm_fail = 0

    total = len(pending)
    done = 0

    def _progress() -> None:
        if cfg.quiet:
            return
        msg = f"{done}/{total} · {findings_total} findings · {contested_total} contested"
        if progress_cb:
            progress_cb("progress", {
                "done": done, "total": total,
                "findings": findings_total, "contested": contested_total,
            })
        else:
            print(msg, file=__import__("sys").stderr)

    # Sequential path (default). Concurrent path still shares one service;
    # NER is serialized behind GlinerBackend._infer_lock.
    if workers <= 1:
        for doc in pending:
            use_llm = cfg.use_llm and not circuit_open
            force = "circuit breaker open after consecutive LLM failures" if circuit_open else ""
            entry = _handle(doc, use_llm)
            if entry.get("status") == "error" and not cfg.continue_on_error:
                _after(entry)
                result.exit_code = 1
                break
            _after(entry, force_llm_unavailable=force)
            done += 1
            _progress()
    else:
        # Chunk so the circuit breaker can still trip between waves.
        idx = 0
        while idx < len(pending):
            if not cfg.continue_on_error and result.errors:
                break
            wave = pending[idx: idx + workers]
            idx += len(wave)
            use_llm = cfg.use_llm and not circuit_open
            force = "circuit breaker open after consecutive LLM failures" if circuit_open else ""
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_handle, doc, use_llm): doc for doc in wave}
                for fut in as_completed(futs):
                    entry = fut.result()
                    if entry.get("status") == "error" and not cfg.continue_on_error:
                        _after(entry)
                        result.exit_code = 1
                        pending = []  # stop further waves
                        break
                    _after(entry, force_llm_unavailable=force)
                    done += 1
                    _progress()

    score_dist = {
        et: {
            "count": len(vals),
            "min": round(min(vals), 4) if vals else 0,
            "max": round(max(vals), 4) if vals else 0,
            "mean": round(sum(vals) / len(vals), 4) if vals else 0,
        }
        for et, vals in score_dist_items(score_buckets)
    }
    scanned = sum(1 for d in result.documents if d.get("status") == "ok")
    errored = len(result.errors)
    skipped = len(result.skipped)
    result.aggregates = {
        "documents": {
            "total": scanned + errored + skipped,
            "scanned": scanned,
            "errored": errored,
            "skipped": skipped,
        },
        "entity_counts": entity_counts,
        "score_distribution": score_dist,
        "engines_ran": sorted(engines_ran),
        "engines_unavailable": dict(engines_unavailable),
        "arbitration": arb_totals,
        "findings": findings_total,
        "contested": contested_total,
        "coverage": coverage_block,
        "llm_circuit_open": circuit_open,
    }
    if cfg.require_llm and cfg.use_llm and (
        llm_unavailable_run or circuit_open or "llm" in engines_unavailable
        or (scanned and "llm" not in engines_ran)
    ):
        result.exit_code = 3
    elif cfg.require_ner and "ner" in engines_unavailable:
        result.exit_code = 3
    elif errored and not cfg.continue_on_error:
        result.exit_code = 1
    elif errored:
        result.exit_code = max(result.exit_code, 0)
    return result


def score_dist_items(buckets: dict[str, list[float]]):
    return sorted(buckets.items())


def _write_redacted(svc, doc: BatchDocument, scan, cfg: BatchRunConfig, out_dir: Path) -> None:
    from redibis.pii.deid.applier import DeidApplier

    policy = None
    if cfg.policy_id:
        policy = svc.get_policy(cfg.policy_id)
        if policy is None and cfg.policy_id in ("default", "full-redact"):
            from redibis.pii.deid.policy import DeidPolicy
            policy = DeidPolicy.redact_all()
            svc.register_policy(policy)
    if policy is None:
        raise RuntimeError(
            f"deidentify requested but policy {cfg.policy_id!r} is not registered"
        )
    dest_dir = (out_dir / "redacted").resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{doc.sanitized_id}.txt"
    source_path = Path(doc.source)
    if source_path.exists():
        source_dir = source_path.resolve().parent if source_path.is_file() else source_path.resolve()
        if dest.resolve().parent == source_dir or dest_dir == source_dir:
            raise RuntimeError("refusing to write redacted output into the input directory")
    deid = DeidApplier().apply(doc.text, scan, policy)
    dest.write_text(deid.deidentified_text, encoding="utf-8")


def write_findings_csv(
    documents_payloads: Sequence[dict[str, Any]],
    dest: Path,
    *,
    include_text: bool,
) -> None:
    fields = [
        "doc_id", "start", "end", "entity_type", "score",
        "engine", "validator", "agreement", "llm_verdict", "llm_reason",
    ]
    if include_text:
        fields.append("text")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for payload in documents_payloads:
            doc_id = payload.get("id") or payload.get("sanitized_id") or ""
            for span in payload.get("spans") or payload.get("detections") or []:
                row = {
                    "doc_id": doc_id,
                    "start": span.get("start"),
                    "end": span.get("end"),
                    "entity_type": span.get("entity_type"),
                    "score": span.get("score"),
                    "engine": span.get("engine"),
                    "validator": span.get("validator"),
                    "agreement": span.get("agreement"),
                    "llm_verdict": span.get("llm_verdict"),
                    "llm_reason": span.get("llm_reason"),
                }
                if include_text:
                    row["text"] = span.get("text") or ""
                writer.writerow(row)


def build_scan_report(
    run: BatchRunResult,
    *,
    cfg: BatchRunConfig,
    include_text: bool,
) -> dict[str, Any]:
    report = {
        "kind": "redibis.text_batch_report",
        "manifest": dict(run.manifest),
        "config": {
            "language": cfg.language,
            "engines": cfg.engines,
            "min_score": cfg.min_score,
            "equation": cfg.equation,
            "use_llm": cfg.use_llm,
            "llm_provider": cfg.llm_provider,
            "llm_model": cfg.llm_model,
            "llm_endpoint_host": _endpoint_host(cfg.llm_endpoint),
            "workers": cfg.workers,
            "max_chars": cfg.max_chars,
        },
        "aggregates": dict(run.aggregates),
        "documents": [
            {k: v for k, v in row.items() if k != "spans"}
            for row in run.documents
        ],
        "errors": list(run.errors),
        "skipped": list(run.skipped),
    }
    if include_text:
        report["note"] = "include-text was requested; matched substrings may appear in document files"
    return report


def _endpoint_host(url: str) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def render_text_batch_html(report: Mapping[str, Any]) -> str:
    from redibis.pii.eval.report_html import report_css, _esc

    agg = report.get("aggregates") or {}
    docs = agg.get("documents") or {}
    entities = agg.get("entity_counts") or {}
    arb = agg.get("arbitration") or {}
    entity_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in sorted(entities.items(), key=lambda kv: (-int(kv[1]), kv[0]))
    ) or "<tr><td colspan='2'>None</td></tr>"
    error_rows = "".join(
        f"<tr><td>{_esc(e.get('id'))}</td><td>{_esc(e.get('error'))}</td></tr>"
        for e in (report.get("errors") or [])
    ) or "<tr><td colspan='2'>None</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Redibis text-batch scan report</title>
<style>{report_css()}</style></head>
<body>
<h1>Batch text PII scan</h1>
<div class="kpis">
  <div class="tile"><div class="lbl">scanned</div><div class="num">{_esc(docs.get('scanned', 0))}</div></div>
  <div class="tile"><div class="lbl">errored</div><div class="num">{_esc(docs.get('errored', 0))}</div></div>
  <div class="tile"><div class="lbl">findings</div><div class="num">{_esc(agg.get('findings', 0))}</div></div>
  <div class="tile"><div class="lbl">contested</div><div class="num">{_esc(agg.get('contested', 0))}</div></div>
</div>
<div class="card"><h2>Arbitration</h2>
<p>mode {_esc((report.get('config') or {}).get('equation'))} ·
contested {_esc(arb.get('contested', 0))} ·
confirmed {_esc(arb.get('confirmed', 0))} ·
vetoed {_esc(arb.get('vetoed', 0))} ·
type conflicts {_esc(arb.get('type_conflicts', 0))}</p>
</div>
<div class="card"><h2>Entities</h2>
<table class="spans"><thead><tr><th>Type</th><th>Count</th></tr></thead>
<tbody>{entity_rows}</tbody></table></div>
<div class="card"><h2>Errors</h2>
<table class="spans"><thead><tr><th>Id</th><th>Error</th></tr></thead>
<tbody>{error_rows}</tbody></table></div>
</body></html>
"""
