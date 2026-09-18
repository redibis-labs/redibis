"""Copy a reviewed contract (and its overlays) into another workspace."""

from __future__ import annotations

from typing import Any

from redibis.store.merger import make_provenance_entry
from redibis.workspace.model import DEFAULT_SLUG, WorkspaceDenied
from redibis.workspace.stores import WorkspaceStores, stores_for

_OVERLAY_KEYS = (
    "_meta/reviews/{table}.json",
    "_meta/pii_decisions/{table}.json",
    "_meta/definition_decisions/{table}.json",
    "_meta/field_decisions/{table}.json",
    "_meta/quality_decisions/{table}.json",
)
_OVERLAY_PREFIXES = (
    "_meta/generations/{table}/",
    "_meta/profiles/{table}/",
    "_meta/steward_artifacts/{table}/",
    "_meta/telemetry/{table}/",
    "audit/{table}/",
)


def _copy_key(src: WorkspaceStores, dst: WorkspaceStores, key: str) -> bool:
    if not src.backend.exists(src.bucket, key):
        return False
    data = src.backend.get_bytes(src.bucket, key)
    content_type = "application/octet-stream"
    if key.endswith(".json"):
        content_type = "application/json"
    elif key.endswith(".yaml") or key.endswith(".yml"):
        content_type = "application/x-yaml"
    dst.backend.put_bytes(dst.bucket, key, data, content_type=content_type)
    return True


def _copy_prefix(src: WorkspaceStores, dst: WorkspaceStores, prefix: str) -> int:
    n = 0
    for key in src.backend.list_keys(src.bucket, prefix=prefix):
        if _copy_key(src, dst, key):
            n += 1
    return n


def promote_table(
    source: WorkspaceStores,
    table: str,
    *,
    target: str = DEFAULT_SLUG,
    force: bool = False,
    actor: str = "",
) -> dict[str, Any]:
    """Copy contract + overlays into *target*. Refuses unless guaranteed, unless force."""
    active = source.contract.get_active(table)
    if active is None:
        raise ValueError(f"no active contract for {table!r}")
    review = source.review.get(table)
    if not review.guaranteed and not force:
        from redibis.services.review_service import _iter_props
        cols = [n for n, _ in _iter_props(active)]
        blockers = source.review.guarantee_blockers(
            table, required_columns=cols,
        )
        raise WorkspaceDenied(
            f"promote refused: steward guarantee does not hold ({blockers})"
        )
    dest = stores_for(target)
    dest.contract.upsert(
        active, table=table, workflow="promote",
        run_id=f"promote-{source.ref.slug}",
    )
    copied = 0
    for tmpl in _OVERLAY_KEYS:
        if _copy_key(source, dest, tmpl.format(table=table)):
            copied += 1
    for tmpl in _OVERLAY_PREFIXES:
        copied += _copy_prefix(source, dest, tmpl.format(table=table))
    digest = ""
    try:
        from redibis.review.artifacts import list_artifacts as list_steward
        digest = str((list_steward(source.contract, table) or {}).get("review_digest") or "")
    except Exception:
        digest = ""
    entry = make_provenance_entry(active, "promote", f"promote-{source.ref.slug}")
    entry.update({
        "source_workspace": source.ref.slug,
        "source_root": source.ref.root,
        "forced": bool(force),
        "review_digest": digest,
        "actor": actor or "",
        "review_guaranteed": bool(review.guaranteed),
    })
    dest.contract.metadata.append_provenance(table, entry)
    dest.touch(table)
    source.touch(table)
    return {
        "table": table,
        "source": source.ref.slug,
        "target": dest.ref.slug,
        "forced": bool(force),
        "copied_keys": copied,
        "guaranteed": bool(review.guaranteed),
    }
