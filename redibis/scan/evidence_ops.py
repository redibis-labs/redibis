"""Evidence-bundle I/O: load, strip/store, replay ``decide_pii``, export.

Library surface used by ``redibis scan evidence|decide`` and tests. Loading a
bundle never reads the source table — replay is evidence-only (PLAN §5.3).
"""

from __future__ import annotations

import copy
import json
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from redibis.models import EQUATION_VOTER_IDS, PIIDetection
from redibis.pii.equations import decide_pii
from redibis.pii.thresholds import Thresholds
from redibis.store.run_output_writer import (
    EVIDENCE_META_PREFIX,
    RunOutputWriter,
)
from redibis.store.storage_backend import StorageBackend

BUNDLE_FILENAME = "evidence_bundle.json"
SHAREABLE_BUNDLE_FILENAME = "evidence_bundle.shareable.json"
BUNDLE_FILENAMES = (BUNDLE_FILENAME, SHAREABLE_BUNDLE_FILENAME)
STRIPPED_BY = "redibis scan evidence store"

DECIDE_PRESETS: dict[str, dict[str, Any]] = {
    "investigation": {
        "equation": "lenient",
        "thresholds": {
            "presidio_min": 0.45,
            "gliner_min": 0.30,
            "phone_min": 0.60,
        },
    },
    "reporting": {
        "equation": "balanced",
        "thresholds": {},
    },
    "audit": {
        "equation": "strict",
        "thresholds": {
            "presidio_min": 0.92,
            "gliner_min": 0.85,
        },
    },
}


class EvidenceError(Exception):
    """Evidence load / store / replay failure."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_safe(table: str) -> str:
    return (table or "").replace(".", "_")


def _load_json_path(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read evidence bundle: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvidenceError(f"evidence bundle is not a JSON object: {path}")
    return data


def _bundle_table(bundle: dict) -> str:
    table = bundle.get("table") if isinstance(bundle.get("table"), dict) else {}
    return str(table.get("name") or "")


def _bundle_run_id(bundle: dict) -> str:
    table = bundle.get("table") if isinstance(bundle.get("table"), dict) else {}
    return str(table.get("run_id") or "")


def _stack_uuid(bundle: dict) -> str:
    header = bundle.get("header") if isinstance(bundle.get("header"), dict) else {}
    prov = header.get("provenance") if isinstance(header.get("provenance"), dict) else {}
    stack = prov.get("pack_stack") if isinstance(prov.get("pack_stack"), dict) else {}
    return str(stack.get("stack_uuid") or "")


# ── locate / load ────────────────────────────────────────────────────────────


def list_local_evidence_paths(output_dir: Path) -> list[Path]:
    """All evidence bundle files under *output_dir* (deterministic order)."""
    root = Path(output_dir)
    if not root.exists():
        return []
    found: set[Path] = set()
    for name in BUNDLE_FILENAMES:
        found.update(p for p in root.rglob(name) if p.is_file())
    return sorted(found)


def _session_evidence_paths(output_dir: Path, table: str) -> list[Path]:
    """Locate bundles under ``scan_output/{session}/runs/{run_id}/`` for *table*."""
    root = Path(output_dir)
    if not root.exists():
        return []
    session_files: list[Path] = []
    direct = root / "session.json"
    if direct.is_file():
        session_files.append(direct)
    else:
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                candidate = child / "session.json"
                if candidate.is_file():
                    session_files.append(candidate)
        except OSError:
            return []

    paths: list[Path] = []
    for session_file in session_files:
        try:
            sess = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(sess.get("table_name") or "") != table:
            continue
        runs_dir = session_file.parent / "runs"
        if not runs_dir.is_dir():
            continue
        for run_dir in sorted(runs_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            for name in BUNDLE_FILENAMES:
                candidate = run_dir / name
                if candidate.is_file():
                    paths.append(candidate)
    return sorted(set(paths))


def _local_paths_for_table(output_dirs: Iterable[Path], table: str) -> list[Path]:
    seen: set[str] = set()
    paths: list[Path] = []
    for root in output_dirs:
        for path in list_local_evidence_paths(root):
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                paths.append(path)
        for path in _session_evidence_paths(root, table):
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                paths.append(path)
    return sorted(paths)


def _collect_evidence_candidates(
    *,
    table: str,
    output_dirs: Optional[Iterable[Path]] = None,
    backend: Optional[StorageBackend] = None,
    bucket: str = "pii-reports",
) -> list[tuple[str, dict, str]]:
    """Return ``(run_id, bundle, hint)`` candidates for *table*."""
    candidates: list[tuple[str, dict, str]] = []
    dirs = [Path(d) for d in (output_dirs or []) if Path(d).exists()]
    if dirs:
        for path in _local_paths_for_table(dirs, table):
            try:
                bundle = _load_json_path(path)
            except EvidenceError:
                continue
            bundle_table = _bundle_table(bundle)
            if bundle_table and bundle_table != table:
                continue
            rid = _bundle_run_id(bundle) or path.parent.name
            candidates.append((rid, bundle, str(path)))

    if backend is not None:
        for key in iter_stored_evidence_keys(backend, bucket, table=table):
            try:
                bundle = backend.get_json(bucket, key)
            except (KeyError, OSError, json.JSONDecodeError):
                continue
            if not isinstance(bundle, dict):
                continue
            if _bundle_table(bundle) and _bundle_table(bundle) != table:
                continue
            rid = _bundle_run_id(bundle) or Path(key).stem
            candidates.append((rid, bundle, f"s3://{bucket}/{key}"))
        table_safe = _table_safe(table)
        from redibis.evidence.paths import WORKFLOW_PREFIXES, storage_prefix_for_key

        for wf in WORKFLOW_PREFIXES:
            prefix = f"{wf}/{table_safe}/"
            try:
                keys = backend.list_keys(bucket, prefix=prefix)
            except Exception:
                continue
            for key in keys:
                if not key.endswith(BUNDLE_FILENAME) and not key.endswith(SHAREABLE_BUNDLE_FILENAME):
                    continue
                try:
                    bundle = backend.get_json(bucket, key)
                except Exception:
                    continue
                if not isinstance(bundle, dict) or (
                    _bundle_table(bundle) and _bundle_table(bundle) != table
                ):
                    continue
                rid = _bundle_run_id(bundle)
                if not rid:
                    parsed = storage_prefix_for_key(key)
                    rid = (parsed or "").rstrip("/").rsplit("/", 1)[-1]
                candidates.append((rid, bundle, f"s3://{bucket}/{key}"))
    return candidates


def iter_stored_evidence_keys(
    backend: StorageBackend, bucket: str, *, table: str | None = None,
) -> list[str]:
    prefix = f"{EVIDENCE_META_PREFIX}/"
    if table:
        prefix = f"{EVIDENCE_META_PREFIX}/{table}/"
    return [
        k for k in backend.list_keys(bucket, prefix=prefix)
        if k.endswith(".json")
    ]


def list_evidence_runs(
    *,
    table: str,
    output_dir: Optional[Path] = None,
    backend: Optional[StorageBackend] = None,
    bucket: str = "pii-reports",
) -> list[str]:
    """Run ids that have evidence for *table*, newest last (same clock as ``--latest``)."""
    candidates: list[tuple[str, dict, str]] = []
    seen: set[str] = set()

    def _add(run_id: str, bundle: dict, hint: str) -> None:
        rid = (run_id or "").strip()
        if rid and rid not in seen:
            seen.add(rid)
            candidates.append((rid, bundle, hint))

    if output_dir is not None:
        for path in list_local_evidence_paths(output_dir):
            try:
                bundle = _load_json_path(path)
            except EvidenceError:
                continue
            if _bundle_table(bundle) == table:
                _add(_bundle_run_id(bundle) or path.parent.name, bundle, str(path))
        for path in _session_evidence_paths(output_dir, table):
            try:
                bundle = _load_json_path(path)
            except EvidenceError:
                continue
            if _bundle_table(bundle) == table or not _bundle_table(bundle):
                _add(_bundle_run_id(bundle) or path.parent.name, bundle, str(path))

    if backend is not None:
        for key in iter_stored_evidence_keys(backend, bucket, table=table):
            try:
                bundle = backend.get_json(bucket, key)
            except Exception:
                bundle = {}
            name = Path(key).stem
            if isinstance(bundle, dict) and (
                not _bundle_table(bundle) or _bundle_table(bundle) == table
            ):
                _add(_bundle_run_id(bundle) or name, bundle if isinstance(bundle, dict) else {}, f"s3://{bucket}/{key}")
        table_safe = _table_safe(table)
        from redibis.evidence.paths import WORKFLOW_PREFIXES, storage_prefix_for_key

        for wf in WORKFLOW_PREFIXES:
            prefix = f"{wf}/{table_safe}/"
            try:
                keys = backend.list_keys(bucket, prefix=prefix)
            except Exception:
                continue
            for key in keys:
                if not key.endswith(BUNDLE_FILENAME) and not key.endswith("evidence_bundle.shareable.json"):
                    continue
                try:
                    bundle = backend.get_json(bucket, key)
                except Exception:
                    continue
                if not isinstance(bundle, dict):
                    continue
                if _bundle_table(bundle) and _bundle_table(bundle) != table:
                    continue
                rid = _bundle_run_id(bundle)
                if not rid:
                    parsed = storage_prefix_for_key(key)
                    rid = (parsed or "").rstrip("/").rsplit("/", 1)[-1]
                _add(rid, bundle, f"s3://{bucket}/{key}")

    candidates.sort(key=_latest_candidate_key)
    return [c[0] for c in candidates]


def load_evidence_bundle(
    *,
    table: str,
    run_id: str | None = None,
    latest: bool = True,
    output_dir: Optional[Path] = None,
    output_dirs: Optional[Iterable[Path]] = None,
    backend: Optional[StorageBackend] = None,
    bucket: str = "pii-reports",
) -> tuple[dict, str]:
    """Return ``(bundle, source_hint)``. Default ``--latest`` when *run_id* is omitted."""
    wanted = (run_id or "").strip() or None
    if wanted is None and not latest:
        raise EvidenceError("pass --run-id or --latest")

    dirs: list[Path] = []
    if output_dirs is not None:
        dirs.extend(Path(d) for d in output_dirs)
    elif output_dir is not None:
        dirs.append(Path(output_dir))

    candidates = _collect_evidence_candidates(
        table=table,
        output_dirs=dirs,
        backend=backend,
        bucket=bucket,
    )

    if not candidates:
        raise EvidenceError(f"no evidence bundle found for table {table!r}")

    if wanted:
        matches = [c for c in candidates if c[0] == wanted]
        if not matches:
            raise EvidenceError(
                f"no evidence bundle for {table!r} run_id={wanted!r}"
            )
        matches.sort(key=lambda c: (
            1 if str(c[2]).startswith("s3://") else 0,
        ))
        rid, bundle, hint = matches[0]
        return bundle, hint

    candidates.sort(key=_latest_candidate_key)
    rid, bundle, hint = candidates[-1]
    return bundle, hint


def load_evidence_manifest(
    *,
    table: str,
    run_id: str,
    hint: str = "",
    output_dirs: Optional[Iterable[Path]] = None,
    backend: Optional[StorageBackend] = None,
    bucket: str = "pii-reports",
) -> Optional[dict]:
    """Best-effort sibling ``evidence_manifest.json`` for a bundle located at *hint*.

    Returns ``None`` (never raises) when no manifest can be found locally or
    in the run's object-store prefix — the review payload degrades gracefully
    without the extra artifact/LLM-call detail a manifest provides.
    """
    if hint and not str(hint).startswith("s3://"):
        candidate = Path(hint).parent / "evidence_manifest.json"
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

    dirs = [Path(d) for d in (output_dirs or []) if Path(d).exists()]
    for root in dirs:
        for path in root.rglob("evidence_manifest.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(data.get("table") or "") == table and str(data.get("run_id") or "") == run_id:
                return data

    if backend is None:
        return None

    if hint.startswith("s3://") and (hint.endswith(BUNDLE_FILENAME) or hint.endswith(SHAREABLE_BUNDLE_FILENAME)):
        key = hint[len(f"s3://{bucket}/"):] if hint.startswith(f"s3://{bucket}/") else ""
        if key:
            sibling = key.rsplit("/", 1)[0] + "/evidence_manifest.json"
            try:
                data = backend.get_json(bucket, sibling)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

    table_safe = _table_safe(table)
    try:
        from redibis.evidence.paths import WORKFLOW_PREFIXES
    except ImportError:
        return None
    for wf in WORKFLOW_PREFIXES:
        prefix = f"{wf}/{table_safe}/"
        try:
            keys = backend.list_keys(bucket, prefix=prefix)
        except Exception:
            continue
        for key in keys:
            if not key.endswith("evidence_manifest.json"):
                continue
            try:
                data = backend.get_json(bucket, key)
            except Exception:
                continue
            if isinstance(data, dict) and str(data.get("run_id") or "") == run_id:
                return data
    return None


def _bundle_completed_at(bundle: dict, hint: str) -> str:
    ts = bundle.get("timestamps") if isinstance(bundle.get("timestamps"), dict) else {}
    for key in ("scan_finished_at", "bundle_created_at", "scan_started_at"):
        val = ts.get(key)
        if val:
            return str(val)
    for key in ("created_at", "completed_at"):
        val = bundle.get(key)
        if val:
            return str(val)
    if hint and not str(hint).startswith("s3://"):
        man = Path(hint).parent / "evidence_manifest.json"
        if man.is_file():
            try:
                data = json.loads(man.read_text(encoding="utf-8"))
                if data.get("created_at"):
                    return str(data["created_at"])
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        try:
            return datetime.fromtimestamp(
                Path(hint).stat().st_mtime, timezone.utc,
            ).isoformat()
        except OSError:
            pass
    return ""


def _latest_candidate_key(candidate: tuple[str, dict, str]) -> tuple:
    rid, bundle, hint = candidate
    ts = _bundle_completed_at(bundle, hint)
    local = 1 if not str(hint).startswith("s3://") else 0
    return (ts, rid, local)


# ── strip / store ────────────────────────────────────────────────────────────


def _strip_top_values(profile: dict | None) -> dict | None:
    if not isinstance(profile, dict):
        return profile
    freq = profile.get("frequency")
    if not isinstance(freq, dict) or not freq.get("top_values"):
        return profile
    out = dict(profile)
    freq_out = dict(freq)
    freq_out["top_values"] = [
        {k: v for k, v in item.items() if k != "value"}
        for item in freq["top_values"]
        if isinstance(item, dict)
    ]
    out["frequency"] = freq_out
    return out


def _mask_literal_values(bundle: dict) -> dict:
    """Replace sample / top-value literals with MaskingEngine output."""
    import pandas as pd

    from redibis.masking.engine import MaskingEngine, RunKeys
    from redibis.masking.plan import auto_suggest_plan

    columns = bundle.get("columns") if isinstance(bundle.get("columns"), dict) else {}
    if not columns:
        return bundle

    detections: list[dict] = []
    sample_lists: dict[str, list] = {}
    top_lists: dict[str, list] = {}
    for col, block in columns.items():
        if not isinstance(block, dict):
            continue
        samples = (block.get("samples") or {}).get("values") or []
        sample_lists[col] = list(samples) if isinstance(samples, list) else []
        freq = ((block.get("profile") or {}).get("frequency") or {})
        tops = []
        for item in freq.get("top_values") or []:
            if isinstance(item, dict) and "value" in item:
                tops.append(item["value"])
        top_lists[col] = tops
        verdict = block.get("pii_verdict") or {}
        detections.append({
            "column": col,
            "detected": bool(verdict.get("detected")),
            "entity_type": verdict.get("entity_type"),
        })

    max_len = max(
        (len(sample_lists.get(c, [])) + len(top_lists.get(c, [])) for c in columns),
        default=0,
    )
    if max_len == 0:
        return bundle

    data: dict[str, list] = {}
    for col in columns:
        vals = list(sample_lists.get(col, [])) + list(top_lists.get(col, []))
        vals.extend([None] * (max_len - len(vals)))
        data[col] = vals
    df = pd.DataFrame(data)
    table_name = _bundle_table(bundle) or "unknown.table"
    plan = auto_suggest_plan(table_name, list(columns), detections)
    # Force a one-way hash so no source literal can survive (fake/FPE can
    # theoretically collide with a real value; hash of the cell cannot).
    plan.columns = [
        replace(
            rule,
            strategy="hash",
            params={"algo": "sha256", "truncate": 16},
            note=(rule.note + " evidence-store-mask").strip(),
        )
        for rule in plan.columns
    ]
    seed = str((bundle.get("table") or {}).get("run_id") or "evidence-store")
    keys = RunKeys.mint(run_id=f"evidence-store-{seed}", seed=seed)
    masked = MaskingEngine(plan, keys).transform_dataframe(df)

    out = copy.deepcopy(bundle)
    out_cols = out.get("columns") or {}
    for col, block in out_cols.items():
        if not isinstance(block, dict):
            continue
        n_samples = len(sample_lists.get(col, []))
        n_tops = len(top_lists.get(col, []))
        series = masked[col] if col in masked.columns else None
        if series is not None and n_samples:
            block.setdefault("samples", {})
            block["samples"]["values"] = [
                None if v is None or (isinstance(v, float) and v != v) else v
                for v in series.iloc[:n_samples].tolist()
            ]
            block["samples"]["mode"] = "masked"
        if series is not None and n_tops:
            profile = block.get("profile") if isinstance(block.get("profile"), dict) else None
            if profile is not None:
                freq = profile.get("frequency") if isinstance(profile.get("frequency"), dict) else None
                if freq is not None:
                    new_tops = []
                    masked_tops = series.iloc[n_samples:n_samples + n_tops].tolist()
                    for i, item in enumerate(freq.get("top_values") or []):
                        if not isinstance(item, dict):
                            continue
                        entry = dict(item)
                        if i < len(masked_tops):
                            entry["value"] = masked_tops[i]
                        new_tops.append(entry)
                    freq["top_values"] = new_tops
                    profile["frequency"] = freq
                    block["profile"] = profile
    out["columns"] = out_cols
    return out


def strip_raw_values(bundle: dict, *, keep_masked_samples: bool = False) -> dict:
    """Remove (or mask) literal source values. Shape of samples/frequency stays."""
    if keep_masked_samples:
        out = _mask_literal_values(bundle)
        sample_mode = "masked"
    else:
        out = copy.deepcopy(bundle)
        columns = out.get("columns") if isinstance(out.get("columns"), dict) else {}
        for block in columns.values():
            if not isinstance(block, dict):
                continue
            samples = block.get("samples")
            if isinstance(samples, dict):
                samples = dict(samples)
                samples.pop("values", None)
                samples["mode"] = "none"
                block["samples"] = samples
            if "profile" in block:
                block["profile"] = _strip_top_values(block.get("profile"))
        out["columns"] = columns
        sample_mode = "none"

    sensitivity = dict(out.get("sensitivity") or {})
    sensitivity["sample_mode"] = sample_mode
    sensitivity["contains_raw_pii"] = False
    sensitivity["egress"] = "allow"
    sensitivity["stripped_at"] = _utc_now_iso()
    sensitivity["stripped_by"] = STRIPPED_BY
    sensitivity["note"] = (
        "Masked sample values; no raw source literals."
        if keep_masked_samples
        else "Raw sample and top-value literals removed."
    )
    out["sensitivity"] = sensitivity
    from redibis.evidence.redact import sanitize_shareable_payload

    return sanitize_shareable_payload(out)


def strip_bundle(*args, **kwargs):
    """Deprecated alias of ``strip_raw_values`` (one-release compatibility)."""
    import warnings

    warnings.warn(
        "strip_bundle is deprecated; use strip_raw_values",
        DeprecationWarning,
        stacklevel=2,
    )
    return strip_raw_values(*args, **kwargs)


def store_evidence(
    bundle: dict,
    writer: RunOutputWriter,
    *,
    keep_masked_samples: bool = False,
) -> tuple[dict, str]:
    """Strip literals and persist via ``RunOutputWriter.write_evidence``."""
    stripped = strip_raw_values(bundle, keep_masked_samples=keep_masked_samples)
    table = _bundle_table(stripped) or writer.table
    raw_writer_id = writer.run_id
    run_id = _bundle_run_id(stripped) or writer.run_id
    from redibis.store.run_output_writer import bare_evidence_run_id

    evidence_rid = bare_evidence_run_id(run_id)
    session_id = ""
    if "/" in str(raw_writer_id or ""):
        session_id = str(raw_writer_id).replace("\\", "/").rsplit("/", 1)[0]
    if session_id:
        table_block = stripped.get("table") if isinstance(stripped.get("table"), dict) else {}
        if table_block is not None:
            table_block = dict(table_block)
            table_block["session_id"] = session_id
            stripped["table"] = table_block
    if writer.table != table or writer.run_id != evidence_rid:
        writer = replace(writer, table=table, run_id=evidence_rid)
    key = writer.write_evidence(stripped)
    return stripped, key


# ── replay / decide ──────────────────────────────────────────────────────────


def _entity_from_block(block: dict | None) -> Optional[str]:
    if not isinstance(block, dict) or block.get("ran") is False:
        return None
    for key in ("entity", "entity_type", "label"):
        val = block.get(key)
        if val:
            text = str(val)
            if key == "label" and (" " in text or text.islower()):
                return text.upper().replace(" ", "_")
            return text
    extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
    for key in ("entity", "entity_type", "verdict"):
        if extra.get(key):
            return str(extra[key])
    hits = block.get("hits") or block.get("pattern_hits") or []
    for hit in hits:
        if isinstance(hit, dict) and hit.get("entity_type"):
            return str(hit["entity_type"])
    entities = block.get("entities") if isinstance(block.get("entities"), dict) else {}
    if entities:
        return str(next(iter(entities)))
    return None


def _entity_from_evidence(ev: dict) -> Optional[str]:
    generic = ev.get("engine_evidence") if isinstance(ev.get("engine_evidence"), dict) else {}
    for eid in ("presidio", "phone", "learned", "gliner", "regex_catalog", "llm_refiner"):
        if eid in generic:
            val = _entity_from_block(generic.get(eid) if isinstance(generic.get(eid), dict) else None)
            if val:
                return val
    if "presidio" not in generic:
        val = _entity_from_block(ev.get("presidio") if isinstance(ev.get("presidio"), dict) else None)
        if val:
            return val
    if "phone" not in generic:
        val = _entity_from_block(ev.get("phone") if isinstance(ev.get("phone"), dict) else None)
        if val:
            return val
    if "learned" not in generic:
        learned = ev.get("learned") if isinstance(ev.get("learned"), dict) else {}
        val = _entity_from_block(learned)
        if val:
            return val
    if "gliner" not in generic:
        val = _entity_from_block(ev.get("gliner") if isinstance(ev.get("gliner"), dict) else None)
        if val:
            return val
    if "regex_catalog" not in generic:
        val = _entity_from_block(ev.get("regex_catalog") if isinstance(ev.get("regex_catalog"), dict) else None)
        if val:
            return val
    return None


def _score_if_ran(block: dict | None, *keys: str):
    if not isinstance(block, dict):
        return None
    if block.get("ran") is False:
        return None
    for key in keys:
        if block.get(key) is not None:
            return block[key]
    return None


def _engine_block(ev: dict, engine_id: str, legacy: dict | None = None) -> dict:
    generic = ev.get("engine_evidence") if isinstance(ev.get("engine_evidence"), dict) else {}
    if engine_id in generic and isinstance(generic.get(engine_id), dict):
        return generic[engine_id]
    return legacy if isinstance(legacy, dict) else {}


def detection_from_evidence(column: str, block: dict) -> PIIDetection:
    """Rebuild ``PIIDetection`` from stored evidence — never from ``pii_verdict``.

    Generic ``engine_evidence`` is preferred; legacy keyed blocks are fallback.
    ``ran is False`` drops scores so replay does not resurrect skipped engines.
    Plugin-only scores are restored onto ``engine_evidence`` but do not vote
    in ``decide_pii``.
    """
    ev = block.get("pii_evidence") if isinstance(block.get("pii_evidence"), dict) else {}
    validators = block.get("validator_results") if isinstance(block.get("validator_results"), dict) else {}
    presidio = _engine_block(ev, "presidio", ev.get("presidio") if isinstance(ev.get("presidio"), dict) else {})
    gliner = _engine_block(ev, "gliner", ev.get("gliner") if isinstance(ev.get("gliner"), dict) else {})
    phone = _engine_block(ev, "phone", ev.get("phone") if isinstance(ev.get("phone"), dict) else {})
    llm = _engine_block(ev, "llm_refiner", ev.get("llm_refiner") if isinstance(ev.get("llm_refiner"), dict) else {})
    learned = _engine_block(ev, "learned", ev.get("learned") if isinstance(ev.get("learned"), dict) else {})
    regex_catalog = _engine_block(
        ev, "regex_catalog", ev.get("regex_catalog") if isinstance(ev.get("regex_catalog"), dict) else {},
    )
    generic_map = ev.get("engine_evidence") if isinstance(ev.get("engine_evidence"), dict) else {}
    nid_rec = _engine_block(ev, "nid")
    imei_rec = _engine_block(ev, "imei")
    imsi_rec = _engine_block(ev, "imsi")
    geo_rec = _engine_block(ev, "geo")

    pattern_hits = []
    if presidio.get("ran") is not False:
        pattern_hits = list(presidio.get("hits") or presidio.get("pattern_hits") or [])
    presidio_pattern = None
    for hit in pattern_hits:
        if isinstance(hit, dict) and hit.get("pattern"):
            presidio_pattern = hit.get("pattern")
            break

    engine_states: dict[str, dict] = {}
    if phone.get("ran") is True:
        engine_states["phone"] = {"ran": True, "enabled": True}
    elif phone.get("ran") is False:
        engine_states["phone"] = {"ran": False, "enabled": True}
    for rec, state_key in (
        (nid_rec, "nid"),
        (imei_rec, "imei"),
        (imsi_rec, "imsi"),
        (geo_rec, "geo"),
    ):
        if rec.get("ran") is False:
            engine_states[state_key] = {"ran": False}
        elif rec.get("ran") is True:
            engine_states[state_key] = {"ran": True}
    for vid, state_key in (
        ("v.eg_nid", "nid"),
        ("v.imei", "imei"),
        ("v.imsi", "imsi"),
        ("v.geo", "geo"),
    ):
        if state_key not in engine_states and vid in validators and state_key not in generic_map:
            engine_states[state_key] = {"ran": True}

    nid = validators.get("v.eg_nid") if isinstance(validators.get("v.eg_nid"), dict) else {}
    imei = validators.get("v.imei") if isinstance(validators.get("v.imei"), dict) else {}
    imsi = validators.get("v.imsi") if isinstance(validators.get("v.imsi"), dict) else {}
    geo = validators.get("v.geo") if isinstance(validators.get("v.geo"), dict) else {}

    regex_hits = pattern_hits
    if not regex_hits and regex_catalog.get("ran") is not False and isinstance(regex_catalog.get("entities"), dict):
        regex_hits = [
            {"entity_type": k, "score": v}
            for k, v in regex_catalog["entities"].items()
        ]
    elif not regex_hits and isinstance(regex_catalog.get("hits"), list):
        regex_hits = list(regex_catalog["hits"]) if regex_catalog.get("ran") is not False else []

    gliner_hits = []
    if gliner.get("ran") is not False:
        gliner_hits = list(gliner.get("hits") or [])

    def _validator_rate(rec: dict, key: str, legacy_block: dict, legacy_field: str):
        rate = _score_if_ran(rec, "score")
        if rate is not None:
            return rate
        if key in generic_map:
            if rec.get("ran") is False:
                return None
            rates = rec.get("rates") if isinstance(rec.get("rates"), dict) else {}
            return rates.get("valid_rate") or rates.get("confidence")
        if engine_states.get(key, {}).get("ran") is False:
            return None
        return legacy_block.get("rate")

    nid_rate = _validator_rate(nid_rec, "nid", nid, "rate")
    imei_rate = _validator_rate(imei_rec, "imei", imei, "rate")
    imsi_rate = _validator_rate(imsi_rec, "imsi", imsi, "rate")
    geo_rate = _validator_rate(geo_rec, "geo", geo, "rate")

    generic = dict(ev.get("engine_evidence") or {})

    llm_verdict = None
    llm_reasoning = None
    if llm.get("ran") is not False:
        extra = llm.get("extra") if isinstance(llm.get("extra"), dict) else {}
        llm_verdict = llm.get("verdict") or extra.get("verdict")
        llm_reasoning = llm.get("reasoning") or extra.get("reasoning")

    return PIIDetection(
        column=column,
        detected=False,
        entity_type=_entity_from_evidence(ev),
        presidio_score=_score_if_ran(presidio, "score"),
        presidio_pattern=presidio_pattern if presidio.get("ran") is not False else None,
        presidio_match_rate=_score_if_ran(presidio, "match_rate"),
        gliner_score=_score_if_ran(gliner, "score"),
        gliner_label=gliner.get("label") if gliner.get("ran") is not False else None,
        gliner_match_rate=_score_if_ran(gliner, "match_rate"),
        llm_score=_score_if_ran(llm, "score"),
        llm_verdict=llm_verdict,
        llm_reasoning=llm_reasoning,
        phone_score=_score_if_ran(phone, "score"),
        phone_entity=phone.get("entity") if phone.get("ran") is not False else None,
        msisdn_valid_rate=(phone.get("msisdn_valid_rate") or (phone.get("rates") or {}).get("msisdn_valid_rate"))
        if phone.get("ran") is not False else None,
        phone_valid_rate=(phone.get("valid_rate") or (phone.get("rates") or {}).get("valid_rate"))
        if phone.get("ran") is not False else None,
        phone_mobile_rate=(phone.get("mobile_rate") or (phone.get("rates") or {}).get("mobile_rate"))
        if phone.get("ran") is not False else None,
        phone_regions=dict(phone.get("regions") or (phone.get("rates") or {}).get("regions") or {}) or None
        if phone.get("ran") is not False else None,
        nid_valid_rate=nid_rate,
        nid_checked=int(nid.get("checked") or (nid_rec.get("rates") or {}).get("checked") or 0),
        imei_valid_rate=imei_rate,
        imei_checked=int(imei.get("checked") or (imei_rec.get("rates") or {}).get("checked") or 0),
        imsi_valid_rate=imsi_rate,
        imsi_checked=int(imsi.get("checked") or (imsi_rec.get("rates") or {}).get("checked") or 0),
        geo_confidence=geo_rate,
        learned_score=_score_if_ran(learned, "score"),
        learned_label=learned.get("label") if learned.get("ran") is not False else None,
        learned_entity=learned.get("entity") or learned.get("entity_type")
        if learned.get("ran") is not False else None,
        learned_engine=(learned.get("engine") or (learned.get("extra") or {}).get("engine"))
        if learned.get("ran") is not False else None,
        regex_hits=regex_hits,
        ner_hits=gliner_hits,
        engine_states=engine_states,
        engine_evidence=generic,
        edge_tags=list(block.get("rules_fired") or []),
    )


def _thresholds_from_preset(
    preset: str,
    *,
    equation: str | None = None,
    overrides: Optional[dict[str, float]] = None,
) -> tuple[str, Thresholds]:
    if preset not in DECIDE_PRESETS:
        raise EvidenceError(
            f"unknown decide preset {preset!r}; "
            f"use {', '.join(sorted(DECIDE_PRESETS))}"
        )
    spec = DECIDE_PRESETS[preset]
    mode = equation or spec["equation"]
    kwargs = dict(spec["thresholds"] or {})
    if overrides:
        kwargs.update(overrides)
    return mode, Thresholds(**kwargs) if kwargs else Thresholds()


def parse_threshold_sets(items: Iterable[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    valid = set(asdict(Thresholds()).keys())
    for raw in items:
        if "=" not in raw:
            raise EvidenceError(f"--set expects KEY=VALUE, got {raw!r}")
        key, _, value = raw.partition("=")
        key = key.strip()
        if key not in valid:
            raise EvidenceError(
                f"unknown threshold {key!r}; valid: {', '.join(sorted(valid))}"
            )
        try:
            out[key] = float(value.strip())
        except ValueError as exc:
            raise EvidenceError(f"invalid threshold value for {key}: {value!r}") from exc
    return out


_EQUATION_EXPRESSIONS = {
    "strict": "ALL available engines vote yes at or above their threshold",
    "balanced": "2-of-N engines vote yes OR one engine >= very_high_confidence_floor",
    "lenient": "any single engine >= its threshold triggers detection",
    "independent": "each engine evaluated independently against its own threshold",
}


def _equation_entry(equation_id: str, mode: str, thresholds: Thresholds, *, default: bool = False) -> dict:
    return {
        "id": equation_id,
        "mode": mode,
        "default": default,
        "expression": _EQUATION_EXPRESSIONS.get(mode, mode),
        "thresholds": {
            k: getattr(thresholds, k)
            for k in (
                "presidio_min", "gliner_min", "llm_min", "phone_min", "nid_min",
                "imei_min", "imsi_min", "geo_min",
            )
            if hasattr(thresholds, k)
        },
    }


def _next_equation_id(existing: list[dict], base: str) -> str:
    ids = {e.get("id") for e in existing if isinstance(e, dict)}
    if base not in ids:
        return base
    n = 2
    while f"{base}.{n}" in ids:
        n += 1
    return f"{base}.{n}"


def _deciding_engines(detection: PIIDetection) -> list[str]:
    names = []
    if hasattr(detection, "deciding_engines"):
        names = detection.deciding_engines()
    elif hasattr(detection, "contributing_engines"):
        names = [
            n for n in detection.contributing_engines()
            if n in EQUATION_VOTER_IDS
        ]
    mapped = []
    for n in names:
        mapped.append({
            "regex": "presidio",
            "ner": "gliner",
            "llm": "llm_refiner",
        }.get(n, n))
    return mapped


def replay(
    bundle: dict,
    *,
    equation: str,
    thresholds: Thresholds,
    equation_id: str | None = None,
    decided_at: str | None = None,
) -> dict:
    """Rebuild detections from ``pii_evidence`` and re-run ``decide_pii``.

    Leaves ``pii_evidence`` byte-identical and copies ``stack_uuid`` unchanged.
    """
    out = copy.deepcopy(bundle)
    header = out.setdefault("header", {})
    rules = header.setdefault("rules", {})
    equations = list(rules.get("equations") or [])
    base_id = equation_id or f"eq.{equation}"
    new_id = _next_equation_id(equations, base_id)
    equations.append(_equation_entry(new_id, equation, thresholds, default=False))
    rules["equations"] = equations
    header["rules"] = rules

    timestamps = dict(header.get("timestamps") or {})
    # phases copied via deepcopy; add decided_at
    timestamps["decided_at"] = decided_at or _utc_now_iso()
    header["timestamps"] = timestamps
    out["header"] = header

    columns = out.get("columns") if isinstance(out.get("columns"), dict) else {}
    detected_count = 0
    by_entity: dict[str, int] = {}
    for col, block in columns.items():
        if not isinstance(block, dict) or "pii_evidence" not in block:
            continue
        detection = detection_from_evidence(col, block)
        verdicted = decide_pii(detection, equation, thresholds)
        block["pii_verdict"] = {
            "derived": True,
            "equation_id": new_id,
            "decided_at": timestamps["decided_at"],
            "detected": bool(verdicted.detected),
            "entity_type": verdicted.entity_type,
            "confidence": float(verdicted.confidence or 0.0),
            "deciding_engines": _deciding_engines(verdicted),
        }
        if verdicted.detected:
            detected_count += 1
            if verdicted.entity_type:
                et = str(verdicted.entity_type)
                by_entity[et] = by_entity.get(et, 0) + 1
        columns[col] = block
    out["columns"] = columns

    summary = dict(out.get("table_summary") or {})
    pii_sum = dict(summary.get("pii") or {})
    scanned = pii_sum.get("columns_scanned")
    if scanned is None:
        scanned = sum(
            1 for b in columns.values()
            if isinstance(b, dict) and "pii_evidence" in b
        )
    pii_sum["columns_scanned"] = scanned
    pii_sum["columns_detected"] = detected_count
    pii_sum["by_entity"] = by_entity
    summary["pii"] = pii_sum
    out["table_summary"] = summary
    return out


def replay_preset(
    bundle: dict,
    *,
    preset: str,
    equation: str | None = None,
    threshold_overrides: Optional[dict[str, float]] = None,
) -> dict:
    mode, thresholds = _thresholds_from_preset(
        preset, equation=equation, overrides=threshold_overrides,
    )
    return replay(
        bundle,
        equation=mode,
        thresholds=thresholds,
        equation_id=f"eq.{preset}",
    )


def _previous_equation_label(bundle: dict) -> str:
    for block in (bundle.get("columns") or {}).values():
        if not isinstance(block, dict):
            continue
        verdict = block.get("pii_verdict") or {}
        eq_id = str(verdict.get("equation_id") or "")
        if eq_id.startswith("eq."):
            return eq_id[3:]
        if eq_id:
            return eq_id
    header = bundle.get("header") or {}
    rules = header.get("rules") or {}
    for entry in rules.get("equations") or []:
        if isinstance(entry, dict) and entry.get("default"):
            return str(entry.get("mode") or entry.get("id") or "previous")
    return "previous"


def format_verdict_diff(
    before: dict,
    after: dict,
    *,
    preset: str | None = None,
) -> str:
    table = _bundle_table(after) or _bundle_table(before) or "table"
    prev_label = _previous_equation_label(before)
    new_label = preset or _previous_equation_label(after)
    cols_before = before.get("columns") if isinstance(before.get("columns"), dict) else {}
    cols_after = after.get("columns") if isinstance(after.get("columns"), dict) else {}
    names = list(dict.fromkeys([*cols_before.keys(), *cols_after.keys()]))

    lines = [f"{table} — {new_label} vs {prev_label}"]
    before_n = 0
    after_n = 0
    for col in names:
        vb = (cols_before.get(col) or {}).get("pii_verdict") or {}
        va = (cols_after.get(col) or {}).get("pii_verdict") or {}
        det_b = bool(vb.get("detected"))
        det_a = bool(va.get("detected"))
        if det_b:
            before_n += 1
        if det_a:
            after_n += 1
        if not det_b and not det_a:
            continue
        ent_a = va.get("entity_type") or "—"
        conf_a = va.get("confidence")
        conf_b = vb.get("confidence")
        conf_a_s = f"{conf_a:.2f}" if isinstance(conf_a, (int, float)) else "—"
        conf_b_s = f"{conf_b:.2f}" if isinstance(conf_b, (int, float)) else "—"
        reason = _diff_reason(col, cols_before.get(col) or {}, cols_after.get(col) or {}, det_b, det_a)
        if det_a and not det_b:
            lines.append(f"  + {col:<12} {ent_a:<16} {conf_a_s}  {reason}")
        elif det_b and not det_a:
            lines.append(f"  - {col:<12} {vb.get('entity_type') or '—':<16} {conf_b_s}  {reason}")
        else:
            arrow = f"{conf_b_s} → {conf_a_s}"
            tag = "unchanged" if conf_b_s == conf_a_s and vb.get("entity_type") == va.get("entity_type") else "changed"
            lines.append(f"  ~ {col:<12} {ent_a:<16} {arrow}  {tag}")
    lines.append(f"  {before_n} → {after_n} columns detected")
    return "\n".join(lines)


def _diff_reason(col: str, before_block: dict, after_block: dict, det_b: bool, det_a: bool) -> str:
    ev = before_block.get("pii_evidence") or after_block.get("pii_evidence") or {}
    if not isinstance(ev, dict):
        return ""
    gliner = ev.get("gliner") or {}
    presidio = ev.get("presidio") or {}
    if det_a and not det_b:
        gscore = gliner.get("score")
        pscore = presidio.get("score")
        if isinstance(gscore, (int, float)) and gscore < 0.70:
            return f"(gliner {gscore:.2f} now votes; was below 0.70)"
        if isinstance(pscore, (int, float)) and pscore < 0.80:
            return f"(regex {pscore:.2f} now votes)"
        return "(now votes at this preset)"
    if det_b and not det_a:
        return "(no longer votes at this preset)"
    return ""


# ── packs listing / export ───────────────────────────────────────────────────


def pack_stack_from_bundle(bundle: dict) -> dict:
    header = bundle.get("header") if isinstance(bundle.get("header"), dict) else {}
    prov = header.get("provenance") if isinstance(header.get("provenance"), dict) else {}
    stack = prov.get("pack_stack") if isinstance(prov.get("pack_stack"), dict) else {}
    return stack


def format_pack_stack(bundle: dict, *, local_uuids: Optional[set[str]] = None) -> str:
    stack = pack_stack_from_bundle(bundle)
    packs = stack.get("packs") if isinstance(stack.get("packs"), list) else []
    stack_uuid = str(stack.get("stack_uuid") or "—")
    short = stack_uuid[:8] + "…" if len(stack_uuid) > 8 else stack_uuid
    mode = stack.get("mode") or "overlay"
    lines = [f"  stack {short}  {mode}  {len(packs)} packs"]
    lines.append(
        f"    {'uuid':<36}  {'kind':<8} {'id':<12} {'version':<8} {'local':<5} contents"
    )
    for p in packs:
        if not isinstance(p, dict):
            continue
        uid = str(p.get("uuid") or "")
        local = p.get("available_locally")
        if local_uuids is not None:
            local = uid in local_uuids
        local_s = "yes" if local else "no"
        contents = p.get("contents") if isinstance(p.get("contents"), dict) else {}
        parts = []
        mapping = (
            ("regex_patterns", "regex"),
            ("custom_rules", "rules"),
            ("ner_entities", "entities"),
            ("validators", "validators"),
            ("behavior_rules", "behavior"),
        )
        for key, label in mapping:
            n = int(contents.get(key) or 0)
            if n:
                parts.append(f"{n} {label}")
        contents_s = " · ".join(parts) if parts else "—"
        lines.append(
            f"    {uid:<36}  {str(p.get('kind') or ''):<8} "
            f"{str(p.get('id') or ''):<12} {str(p.get('version') or ''):<8} "
            f"{local_s:<5} {contents_s}"
        )
    return "\n".join(lines)


def export_with_packs(
    bundle: dict,
    out_zip: Path,
    *,
    pack_store: Any | None = None,
    with_packs: bool = True,
) -> Path:
    """Write ``evidence_bundle.json`` + optional ``packs/{uuid}.zip`` + MANIFEST.sha256."""
    from redibis.pack.canonical import sha256_bytes

    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines: list[str] = []
    bundle_bytes = json.dumps(bundle, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    manifest_lines.append(f"{sha256_bytes(bundle_bytes)}  {BUNDLE_FILENAME}")

    pack_blobs: list[tuple[str, bytes]] = []
    if with_packs and pack_store is not None:
        stack = pack_stack_from_bundle(bundle)
        for entry in stack.get("packs") or []:
            if not isinstance(entry, dict):
                continue
            uid = str(entry.get("uuid") or "").strip()
            if not uid:
                continue
            data = pack_store.get(uid)
            rel = f"packs/{uid}.zip"
            pack_blobs.append((rel, data))
            manifest_lines.append(f"{sha256_bytes(data)}  {rel}")

    manifest_text = "\n".join(manifest_lines) + "\n"
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(BUNDLE_FILENAME, bundle_bytes)
        for rel, data in pack_blobs:
            zf.writestr(rel, data)
        zf.writestr("MANIFEST.sha256", manifest_text.encode("utf-8"))
    return out_zip


def detected_columns(bundle: dict) -> set[str]:
    out: set[str] = set()
    for col, block in (bundle.get("columns") or {}).items():
        if isinstance(block, dict) and (block.get("pii_verdict") or {}).get("detected"):
            out.add(col)
    return out


def apply_supplied_verdicts(
    bundle: dict,
    package_path: Path | str,
    *,
    pii_decisions: Optional[dict[str, dict]] = None,
) -> tuple[dict, dict]:
    """Run-scoped verdict replay — does not mutate durable stores.

    Returns ``(review_payload, warnings)`` where *review_payload* is the
    normalized evidence review model and *warnings* lists non-authoritative columns.
    """
    from redibis.review.verdict_package import VerdictPackageError, load_verdict_package
    from redibis.services.evidence_review_service import EvidenceReviewService

    try:
        package = load_verdict_package(package_path)
    except VerdictPackageError as exc:
        raise EvidenceError(str(exc)) from exc

    svc = EvidenceReviewService()
    review = svc.from_bundle(bundle, supplied_verdicts=package, mode="cli_replay")
    warnings: dict[str, list[str]] = {"stale": [], "missing": [], "malformed": []}
    table = str((bundle.get("table") or {}).get("name") or "")
    supplied = package.by_column(table)
    for column in (bundle.get("columns") or {}):
        if column not in supplied:
            continue
        item = next((c for c in review["columns"] if c["column"] == column), None)
        if item is None:
            warnings["malformed"].append(column)
            continue
        if item["effective_verdict"]["source"] == "supplied":
            continue
        drift_state = (item.get("drift") or {}).get("state")
        if drift_state == "stale":
            warnings["stale"].append(column)
        elif item["effective_verdict"]["source"] == "engine":
            if item.get("steward_decision"):
                warnings["stale"].append(column)
            else:
                warnings["missing"].append(column)
    if pii_decisions:
        for col in review["columns"]:
            if col["column"] in pii_decisions and col["effective_verdict"]["source"] == "steward":
                col["effective_verdict"]["authority_reason"] = "active_local_steward_decision"
    return review, warnings
