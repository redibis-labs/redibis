"""Load per-run contract lifecycle artifacts for catalog push (G6)."""

from __future__ import annotations

from typing import Any, Optional


def load_latest_lifecycle_artifacts(
    backend,
    bucket: str,
    table: str,
    *,
    workflow: str = "enrich",
) -> Optional[dict[str, Any]]:
    """Return the newest enrich-run diff + active ref for a table."""
    table_safe = table.replace(".", "_")
    prefix = f"{workflow}/{table_safe}/"
    run_prefixes: set[str] = set()
    for key in backend.list_keys(bucket, prefix=prefix):
        parts = key[len(prefix):].split("/")
        if parts:
            run_prefixes.add(parts[0])

    if not run_prefixes:
        return None

    latest_run = sorted(run_prefixes)[-1]
    base = f"{prefix}{latest_run}/"
    out: dict[str, Any] = {"run_id": latest_run, "workflow": workflow}

    for name, field in (
        ("contract_diff.md", "diff_md"),
        ("contract_diff.json", "diff_json"),
        ("contract.active.ref.json", "active_ref"),
        ("contract.deterministic.yaml", "deterministic_snapshot"),
        ("contract.llm.yaml", "llm_snapshot"),
    ):
        key = base + name
        if not backend.exists(bucket, key):
            continue
        try:
            if name.endswith(".md"):
                out[field] = backend.get_text(bucket, key)
            elif name.endswith(".json"):
                out[field] = backend.get_json(bucket, key)
            elif name.endswith(".yaml"):
                out[field] = backend.get_yaml(bucket, key)
        except Exception:
            continue

    if not any(k in out for k in ("diff_md", "diff_json")):
        return None
    return out
