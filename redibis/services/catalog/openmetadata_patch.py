"""OpenMetadata remote→assertion mapping and RFC-6902 JSON Patch builder.

Pure helpers (no HTTP). Never patch dataType / owners / tier / domain.

Tag writes rebuild each column's ``tags`` array atomically so sequential
RFC-6902 removes cannot shift indexes mid-patch.
"""

from __future__ import annotations

from typing import Any, Optional

from redibis.services.catalog.assertions import (
    Assertion,
    AssertionKey,
    Authority,
    Facet,
    LedgerEntry,
    value_hash,
)
from redibis.services.catalog.reconcile import IntentOp

_TAG_FACETS = frozenset({Facet.PII_TAG, Facet.POLICY_TAG, Facet.ENTITY_TAG})
_COLUMN_TAG_FACETS = frozenset({*_TAG_FACETS, Facet.GLOSSARY_LINK})
_REDIBIS_TAG_PREFIXES = ("PII.", "Redibis.", "RedibisPolicy.")

# Paths / fields Redibis must never write via enrich PATCH.
_FORBIDDEN_PATH_FRAGMENTS = (
    "/dataType",
    "/owners",
    "/tier",
    "/domain",
    "dataType",
)


def _is_redibis_tag_fqn(tag_fqn: str) -> bool:
    return any(tag_fqn.startswith(p) for p in _REDIBIS_TAG_PREFIXES)


def _is_managed_glossary_tag(tag: dict) -> bool:
    """Redibis glossary term links use ``source=Glossary`` and ``Redibis_*`` FQNs."""
    if str(tag.get("source") or "") != "Glossary":
        return False
    fqn = str(tag.get("tagFQN") or "")
    return fqn.startswith("Redibis_")


def _is_managed_column_tag(tag: dict) -> bool:
    fqn = str(tag.get("tagFQN") or "")
    if _is_redibis_tag_fqn(fqn):
        return True
    return _is_managed_glossary_tag(tag)


def _label_type_authority(
    label_type: str,
    *,
    value: Any,
    ledger_by_key: dict[AssertionKey, LedgerEntry],
    key: AssertionKey,
) -> Authority:
    lt = (label_type or "").strip()
    if lt == "Manual":
        return Authority.HUMAN
    if lt == "Propagated":
        return Authority.PROPAGATED
    if lt == "Derived":
        return Authority.EXTERNAL
    if lt == "Automated":
        led = ledger_by_key.get(key)
        if led is not None and led.value_hash == value_hash(value):
            return Authority.REDIBIS
        return Authority.EXTERNAL
    return Authority.UNKNOWN


def _tag_facet(tag_fqn: str) -> Facet:
    if tag_fqn.startswith("PII."):
        return Facet.PII_TAG
    if tag_fqn.startswith("RedibisPolicy."):
        return Facet.POLICY_TAG
    return Facet.ENTITY_TAG


def remote_assertions_from_table(
    entity: dict,
    *,
    ledger_entries: Optional[list[LedgerEntry]] = None,
) -> list[Assertion]:
    """Map an OM table entity JSON into remote assertions with authority."""
    asset_fqn = str(
        entity.get("fullyQualifiedName") or entity.get("name") or ""
    )
    ledger_by_key = {e.key: e for e in (ledger_entries or [])}
    out: list[Assertion] = []

    table_desc = (entity.get("description") or "").strip()
    if table_desc:
        key = AssertionKey(
            asset_fqn=asset_fqn,
            column_path="",
            facet=Facet.TABLE_DESCRIPTION,
            value_key="",
        )
        led = ledger_by_key.get(key)
        auth = (
            Authority.REDIBIS
            if led is not None and led.value_hash == value_hash(table_desc)
            else Authority.HUMAN
        )
        out.append(
            Assertion(key=key, value=table_desc, authority=auth)
        )

    for col in entity.get("columns") or []:
        if not isinstance(col, dict) or not col.get("name"):
            continue
        col_path = str(col["name"])

        col_desc = (col.get("description") or "").strip()
        if col_desc:
            key = AssertionKey(
                asset_fqn=asset_fqn,
                column_path=col_path,
                facet=Facet.COLUMN_DESCRIPTION,
                value_key="",
            )
            led = ledger_by_key.get(key)
            auth = (
                Authority.REDIBIS
                if led is not None and led.value_hash == value_hash(col_desc)
                else Authority.HUMAN
            )
            out.append(Assertion(key=key, value=col_desc, authority=auth))

        display = (col.get("displayName") or "").strip()
        if display:
            key = AssertionKey(
                asset_fqn=asset_fqn,
                column_path=col_path,
                facet=Facet.COLUMN_DISPLAY_NAME,
                value_key="",
            )
            led = ledger_by_key.get(key)
            auth = (
                Authority.REDIBIS
                if led is not None and led.value_hash == value_hash(display)
                else Authority.HUMAN
            )
            out.append(Assertion(key=key, value=display, authority=auth))

        for tag in col.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            tag_fqn = str(tag.get("tagFQN") or "").strip()
            if not tag_fqn:
                continue
            if _is_managed_glossary_tag(tag):
                facet = Facet.GLOSSARY_LINK
            elif _is_redibis_tag_fqn(tag_fqn):
                facet = _tag_facet(tag_fqn)
            else:
                continue
            key = AssertionKey(
                asset_fqn=asset_fqn,
                column_path=col_path,
                facet=facet,
                value_key=tag_fqn,
            )
            auth = _label_type_authority(
                str(tag.get("labelType") or ""),
                value=tag_fqn,
                ledger_by_key=ledger_by_key,
                key=key,
            )
            out.append(Assertion(key=key, value=tag_fqn, authority=auth))

    return out


def _column_index(entity: dict, column_path: str) -> Optional[int]:
    for i, col in enumerate(entity.get("columns") or []):
        if isinstance(col, dict) and col.get("name") == column_path:
            return i
    return None


def _om_tag_value(tag_fqn: str, *, state: str = "confirmed") -> dict:
    return {
        "tagFQN": tag_fqn,
        "source": "Classification",
        "labelType": "Automated",
        "state": "Confirmed" if state == "confirmed" else "Suggested",
    }


def _om_glossary_tag_value(term_fqn: str, *, state: str = "confirmed") -> dict:
    """Column TagLabel linking a glossary term (atomic rebuild path)."""
    return {
        "tagFQN": term_fqn,
        "source": "Glossary",
        "labelType": "Automated",
        "state": "Confirmed" if state == "confirmed" else "Suggested",
    }


def _path_forbidden(path: str) -> bool:
    lower = path.lower()
    return any(frag.lower() in lower for frag in _FORBIDDEN_PATH_FRAGMENTS)


def _rebuild_column_tags(snapshot_col: dict, ops_for_col: list[IntentOp]) -> list[dict]:
    """Foreign tags preserved verbatim; Redibis classification + glossary tags rebuilt."""
    foreign: list[dict] = []
    keep: dict[str, dict] = {}
    for t in snapshot_col.get("tags") or []:
        if not isinstance(t, dict):
            continue
        fqn = str(t.get("tagFQN") or "")
        if not fqn:
            continue
        if _is_managed_column_tag(t):
            keep[fqn] = t
        else:
            foreign.append(dict(t))
    for op in ops_for_col:
        fqn = op.key.value_key or str(op.value or "")
        if not fqn:
            continue
        if op.op == "remove":
            keep.pop(fqn, None)
        elif op.key.facet is Facet.GLOSSARY_LINK:
            keep[fqn] = _om_glossary_tag_value(fqn, state=op.state)
        else:  # add | update | promote | demote (classification)
            keep[fqn] = _om_tag_value(fqn, state=op.state)
    return foreign + [keep[k] for k in sorted(keep)]


def build_json_patch(
    ops: list[IntentOp],
    entity_snapshot: dict,
) -> list[dict]:
    """Build RFC-6902 ops for Redibis-owned paths only.

    Tag mutations (classification + glossary term links) are emitted as a single
    ``replace`` of ``/columns/{i}/tags`` per affected column (foreign tags
    preserved). This avoids RFC-6902 index shift when multiple tags change on
    one column in one push.

    Never emits dataType / owners / tier / domain patches.
    """
    patch: list[dict] = []

    # Group tag + glossary-link ops by column for atomic rebuild
    tag_ops_by_col: dict[str, list[IntentOp]] = {}
    other_ops: list[IntentOp] = []
    for op in ops:
        if op.key.facet in _COLUMN_TAG_FACETS:
            tag_ops_by_col.setdefault(op.key.column_path, []).append(op)
        else:
            other_ops.append(op)

    for column_path, tag_ops in sorted(tag_ops_by_col.items()):
        col_i = _column_index(entity_snapshot, column_path)
        if col_i is None:
            continue
        col = (entity_snapshot.get("columns") or [])[col_i]
        if not isinstance(col, dict):
            continue
        path = f"/columns/{col_i}/tags"
        if _path_forbidden(path):
            continue
        new_tags = _rebuild_column_tags(col, tag_ops)
        # Skip no-op replaces
        old_fqns = [
            str(t.get("tagFQN") or "")
            for t in (col.get("tags") or [])
            if isinstance(t, dict)
        ]
        new_fqns = [str(t.get("tagFQN") or "") for t in new_tags]
        old_states = {
            str(t.get("tagFQN") or ""): (
                str(t.get("state") or ""),
                str(t.get("source") or ""),
            )
            for t in (col.get("tags") or [])
            if isinstance(t, dict)
        }
        new_states = {
            str(t.get("tagFQN") or ""): (
                str(t.get("state") or ""),
                str(t.get("source") or ""),
            )
            for t in new_tags
        }
        if old_fqns == new_fqns and all(
            old_states.get(f) == new_states.get(f) for f in new_fqns
        ):
            continue
        patch.append({
            "op": "replace",
            "path": path,
            "value": new_tags,
        })

    for op in other_ops:
        facet = op.key.facet
        if facet is Facet.COLUMN_DESCRIPTION:
            col_i = _column_index(entity_snapshot, op.key.column_path)
            if col_i is None or op.op == "remove":
                continue
            path = f"/columns/{col_i}/description"
            if _path_forbidden(path):
                continue
            patch.append({
                "op": "replace" if op.op in ("update", "promote", "demote") else "add",
                "path": path,
                "value": op.value or "",
            })

        elif facet is Facet.COLUMN_DISPLAY_NAME:
            col_i = _column_index(entity_snapshot, op.key.column_path)
            if col_i is None or op.op == "remove":
                continue
            path = f"/columns/{col_i}/displayName"
            if _path_forbidden(path):
                continue
            patch.append({
                "op": "replace" if op.op in ("update", "promote", "demote") else "add",
                "path": path,
                "value": op.value or "",
            })

        elif facet is Facet.TABLE_DESCRIPTION:
            if op.op == "remove":
                continue
            path = "/description"
            if _path_forbidden(path):
                continue
            existing = (entity_snapshot.get("description") or "").strip()
            patch.append({
                "op": "replace" if existing else "add",
                "path": path,
                "value": op.value or "",
            })

        # QUALITY_TEST handled by dedicated publishers

    return [p for p in patch if not _path_forbidden(str(p.get("path") or ""))]


__all__ = [
    "build_json_patch",
    "remote_assertions_from_table",
]
