"""CLI: fetch run artifacts (LLM enrichment evidence bundles)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import yaml

from redibis.services.session.loader import artifact_path_allowed

_LLM_CALL_LOG_FILES = (
    "contract.deterministic.yaml",
    "contract.llm.yaml",
    "llm_prompt_context.json",
    "contract_diff.json",
    "contract_diff.md",
    "enrichment_meta.json",
    "evidence_bundle.shareable.json",
    "evidence_manifest.json",
    "effective_config.yaml",
    "result_variants.json",
    "result_comparison.json",
)


def _shareable_name(name: str) -> bool:
    n = name.replace("\\", "/")
    if n.endswith(".raw.json") or Path(n).name == "evidence_bundle.json":
        return False
    base = Path(n).name
    if base in _LLM_CALL_LOG_FILES or base.endswith(".debug.log"):
        return True
    if n.startswith("llm_calls/") and n.endswith(".json") and not n.endswith(".raw.json"):
        return True
    return False


def _find_run_prefixes(
    backend,
    runs_bucket: str,
    run_id: str,
) -> list[tuple[str, str]]:
    """Return ``(table, storage_prefix)`` pairs matching ``run_id``."""
    from redibis.evidence.paths import WORKFLOW_PREFIXES, prefixes_for_run_id, table_from_prefix

    matches: list[tuple[str, str]] = []
    seen: set[str] = set()
    for workflow in WORKFLOW_PREFIXES:
        try:
            keys = backend.list_keys(runs_bucket, prefix=f"{workflow}/")
        except Exception:
            continue
        for prefix in prefixes_for_run_id(keys, run_id):
            if prefix in seen:
                continue
            seen.add(prefix)
            matches.append((table_from_prefix(prefix), prefix))
    return matches


def _find_enrich_run_prefixes(backend, runs_bucket: str, run_id: str) -> list[tuple[str, str]]:
    return _find_run_prefixes(backend, runs_bucket, run_id)


def _resolve_run_prefixes_from_session(
    session_id: str,
    *,
    backend,
    runs_bucket: str,
) -> list[tuple[str, str]]:
    """Resolve enrich run prefixes from an agent/session run id."""
    from redibis.agents.lineage_store import LineageStore
    from redibis.config import RedibisConfig

    cfg = RedibisConfig.default()
    roots = [
        Path(cfg.agents.runs_dir),
        Path(cfg.report.output_dir) / "agent_runs",
    ]
    run_ids: list[str] = []
    for root in roots:
        store = LineageStore(root)
        try:
            run = store.load(session_id)
        except FileNotFoundError:
            continue
        run_ids.append(run.run_id)
        for step in run.steps or []:
            out = step.output or {}
            rid = out.get("run_id")
            if isinstance(rid, str) and rid.strip():
                run_ids.append(rid.strip())
        break

    if not run_ids:
        run_ids = [session_id]

    prefixes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rid in run_ids:
        for table, prefix in _find_run_prefixes(backend, runs_bucket, rid):
            if prefix not in seen:
                seen.add(prefix)
                prefixes.append((table, prefix))
    return prefixes


def _list_artifact_names(backend, runs_bucket: str, prefix: str, table: str) -> list[str]:
    names = list(_LLM_CALL_LOG_FILES) + [f"{table}.debug.log"]
    try:
        keys = backend.list_keys(runs_bucket, prefix=f"{prefix}llm_calls/")
    except Exception:
        keys = []
    for key in keys:
        rel = key[len(prefix):] if key.startswith(prefix) else key
        if _shareable_name(rel) and rel not in names:
            names.append(rel)
    return names


def _copy_run_artifacts(
    backend,
    runs_bucket: str,
    prefix: str,
    *,
    table: str,
    out_dir: Path,
) -> list[str]:
    manifest: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    names = _list_artifact_names(backend, runs_bucket, prefix, table)

    for name in names:
        if not _shareable_name(name):
            continue
        dest = out_dir / name
        if not artifact_path_allowed(Path(name) if Path(name).is_absolute() else dest, run_dir=out_dir):
            # Known shareable names are still written under out_dir.
            if Path(name).name not in _LLM_CALL_LOG_FILES and not name.startswith("llm_calls/"):
                if not name.endswith(".debug.log"):
                    continue
        key = f"{prefix}{name}"
        if not backend.exists(runs_bucket, key):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith((".yaml", ".yml")):
            content = backend.get_yaml(runs_bucket, key)
            dest.write_text(yaml.dump(content, sort_keys=False, allow_unicode=True), encoding="utf-8")
        elif name.endswith(".json"):
            content = backend.get_json(runs_bucket, key)
            from redibis.evidence.redact import sanitize_shareable_payload

            dest.write_text(
                json.dumps(sanitize_shareable_payload(content), indent=2),
                encoding="utf-8",
            )
        else:
            dest.write_text(backend.get_text(runs_bucket, key), encoding="utf-8")
        manifest.append(str(dest))
    return manifest


def run_get(args, store, backend) -> int:
    if args.get_action != "llm-call-logs":
        return 2

    run_id = str(args.run_or_session_id)
    runs_bucket = getattr(getattr(store, "config", None), "runs_bucket", None) or "pii-reports"
    if hasattr(store, "backend"):
        backend = store.backend

    prefixes = _find_run_prefixes(backend, runs_bucket, run_id)
    if not prefixes:
        prefixes = _resolve_run_prefixes_from_session(
            run_id, backend=backend, runs_bucket=runs_bucket,
        )
    if not prefixes:
        print(f"No run artifacts found for {run_id!r}")
        return 1

    all_manifest: list[str] = []
    if args.zip:
        zip_path = Path(args.zip)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for table, prefix in prefixes:
                names = _list_artifact_names(backend, runs_bucket, prefix, table)
                for name in names:
                    if not _shareable_name(name):
                        continue
                    key = f"{prefix}{name}"
                    if not backend.exists(runs_bucket, key):
                        continue
                    arcname = f"{table}/{name}" if len(prefixes) > 1 else name
                    if name.endswith((".yaml", ".yml")):
                        body = yaml.dump(
                            backend.get_yaml(runs_bucket, key),
                            sort_keys=False,
                            allow_unicode=True,
                        ).encode("utf-8")
                    elif name.endswith(".json"):
                        body = json.dumps(backend.get_json(runs_bucket, key), indent=2).encode("utf-8")
                    else:
                        body = backend.get_text(runs_bucket, key).encode("utf-8")
                    zf.writestr(arcname, body)
                    all_manifest.append(arcname)
        print(json.dumps({"zip": str(zip_path), "files": all_manifest}, indent=2))
        return 0

    out_root = Path(args.out or f"llm-call-logs-{run_id}")
    for table, prefix in prefixes:
        dest = out_root / _table_safe(table) if len(prefixes) > 1 else out_root
        all_manifest.extend(
            _copy_run_artifacts(backend, runs_bucket, prefix, table=table, out_dir=dest)
        )
    print(json.dumps({"out": str(out_root), "files": all_manifest}, indent=2))
    return 0
