"""Publish a text-gateway rule set as an immutable pack version.

Publishing mints a UUID and writes a ``.rdbpack``. It does **not** activate
the pack — activation is a separate audited import into ``PackStackStore``.

The evaluation gate is verified against the run registry: the run must exist,
be kind ``eval``, carry a gate verdict, and its rules checksum must match the
rules being published. Caller-supplied ``eval_gate_passed`` is ignored.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from redibis import __version__ as REDIBIS_VERSION
from redibis.pack.defaults import default_compat_range
from redibis.pack.identity import infer_family_id, infer_pack_kind, mint_pack_uuid
from redibis.pack.models import PackContents, PackManifest, PackMetadata, PackRequires
from redibis.pack.writer import write_pack
from redibis.store.pack_store import PackRef, PackStore
from redibis.store.storage_backend import LocalBackend

PathLike = Union[str, Path]


class PublishError(ValueError):
    """Authoring / gate failure — mapped to HTTP 400 by the API."""


def default_pack_store_dir() -> Path:
    env = os.environ.get("REDIBIS_PACK_STORE_DIR")
    if env:
        return Path(env).expanduser()
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "published-packs"


def default_pack_store() -> PackStore:
    return PackStore(LocalBackend(default_pack_store_dir()), bucket="packs")


def _as_named_docs(raw: Any, *, default_name: str, named: bool) -> dict[str, Any]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        return {default_name: raw}
    if named:
        return {str(k): v for k, v in raw.items() if v is not None}
    return {default_name: raw}


def _published_rules_checksum(rule_docs: Mapping[str, Any]) -> str:
    from redibis.pii.eval.provenance import rules_checksum
    from redibis.pii.text_rules import compile_text_rules, overlay_from_pack_document

    overlay = compile_text_rules(
        *[overlay_from_pack_document(doc) for doc in rule_docs.values()]
    )
    return rules_checksum(overlay)


def _gate_from_run(run: Any) -> tuple[Optional[bool], str]:
    outcome = dict(getattr(run, "outcome", None) or {})
    if outcome.get("gate_passed") is not None:
        summary = outcome.get("eval_gate_summary") or outcome.get("gate") or ""
        if isinstance(summary, dict):
            summary = summary.get("summary") or ""
        return bool(outcome["gate_passed"]), str(summary or "")
    gate = outcome.get("gate")
    if isinstance(gate, dict) and gate.get("passed") is not None:
        return bool(gate["passed"]), str(gate.get("summary") or "")
    if isinstance(gate, str) and gate.strip():
        text = gate.strip()
        if text == "pass" or text.startswith("pass"):
            return True, text
        if text.startswith("fail"):
            return False, text
    return None, ""


def _resolve_eval_run(
    eval_uid: str,
    *,
    published_checksum: str,
    allow_unevaluated: bool,
    unevaluated_reason: str,
    run_store: Any = None,
) -> tuple[Optional[bool], str]:
    """Load the eval run and return (gate_passed, summary). Never trusts the caller."""
    from redibis.pii.run_store import get_run_store

    store = run_store or get_run_store()
    rec = store.get_run(eval_uid)
    if rec is None:
        raise PublishError(f"evaluation run not found: {eval_uid}")
    if rec.kind != "eval":
        raise PublishError(
            f"run {eval_uid} is kind {rec.kind!r}, not 'eval'"
        )
    gate_passed, summary = _gate_from_run(rec)
    got = str((rec.outcome or {}).get("rules_checksum") or "")
    if not got and rec.provenance_uuid:
        prov = store.get_provenance(rec.provenance_uuid)
        if prov is not None:
            got = str(prov.rules_checksum or "")
    if got != published_checksum and not (allow_unevaluated and unevaluated_reason):
        raise PublishError(
            "eval run rules_checksum does not match the rules being published"
        )
    return gate_passed, summary


def publish_text_gateway_pack(
    *,
    rules: Mapping[str, Any] | None = None,
    gazetteers: Mapping[str, Any] | None = None,
    lexicons: Mapping[str, Any] | None = None,
    version: str,
    description: str,
    author: str,
    tenant: str = "",
    pack_id: str = "text-gateway-rules",
    parent_uuid: str | None = None,
    family_id: str | None = None,
    eval_run_uuid: str = "",
    eval_gate_passed: Any = None,  # ignored — taken from the eval run
    eval_gate_summary: str = "",
    allow_unevaluated: bool = False,
    unevaluated_reason: str = "",
    name: str = "operator",
    rules_named: bool = False,
    gazetteers_named: bool = True,
    lexicons_named: bool = True,
    store: PackStore | None = None,
    dest: PathLike | None = None,
    run_store: Any = None,
) -> PackRef:
    """Write an immutable text-gateway pack version and publish it to the store.

    Refuse publishing without an attached, verified evaluation run unless an
    admin override records a reason. A failing or missing gate likewise
    requires ``allow_unevaluated``.
    """
    del eval_gate_passed  # never trusted
    version = str(version or "").strip()
    if not version:
        raise PublishError("version is required (semver)")
    description = str(description or "").strip()
    if not description:
        raise PublishError("description is required — it is the change log")
    eval_uid = str(eval_run_uuid or "").strip()
    reason = str(unevaluated_reason or "").strip()
    if not eval_uid and not (allow_unevaluated and reason):
        raise PublishError(
            "publishing requires an evaluation run (eval_run_uuid); "
            "override only with allow_unevaluated and a recorded reason"
        )

    rule_docs = _as_named_docs(rules, default_name=name or "operator", named=rules_named)
    gaz_docs = _as_named_docs(
        gazetteers, default_name=name or "operator", named=gazetteers_named
    ) if gazetteers else {}
    lex_docs = _as_named_docs(
        lexicons, default_name=name or "operator", named=lexicons_named
    ) if lexicons else {}
    if not rule_docs and not gaz_docs and not lex_docs:
        raise PublishError("nothing to publish: provide rules, gazetteers, or lexicons")

    gate_passed: Optional[bool] = None
    summary = str(eval_gate_summary or "").strip()
    if eval_uid:
        published_checksum = _published_rules_checksum(rule_docs) if rule_docs else ""
        run_gate, run_summary = _resolve_eval_run(
            eval_uid,
            published_checksum=published_checksum,
            allow_unevaluated=allow_unevaluated,
            unevaluated_reason=reason,
            run_store=run_store,
        )
        gate_passed = run_gate
        if run_summary:
            summary = run_summary
        if gate_passed is None and not (allow_unevaluated and reason):
            raise PublishError(
                "evaluation did not record a gate verdict; "
                "override only with allow_unevaluated and a recorded reason"
            )
        if gate_passed is False and not (allow_unevaluated and reason):
            raise PublishError(
                "evaluation gate failed; override only with allow_unevaluated "
                "and a recorded reason"
            )
    elif not (allow_unevaluated and reason):
        raise PublishError("publishing requires an evaluation run (eval_run_uuid)")

    uid = mint_pack_uuid()
    contents = PackContents(
        text_gateway_rules=sorted(rule_docs),
        text_gateway_gazetteers=sorted(gaz_docs),
        text_gateway_lexicons=sorted(lex_docs),
    )
    kind = infer_pack_kind(contents, pack_id=pack_id)
    family = (family_id or "").strip() or infer_family_id(
        author=author or "unknown",
        kind=kind,
        pack_id=pack_id,
    )

    if allow_unevaluated and reason:
        summary = (summary + " | override: " + reason).strip(" |")

    manifest = PackManifest(
        apiVersion="redibis.io/pack/v1",
        kind="RedibisPack",
        metadata=PackMetadata(
            id=str(pack_id or "text-gateway-rules"),
            version=version,
            uuid=uid,
            family_id=family,
            parent_uuid=(str(parent_uuid).strip() or None) if parent_uuid else None,
            description=description,
            author=str(author or ""),
            tenant=str(tenant or "") or None,
            eval_run_uuid=eval_uid or None,
            eval_gate_passed=gate_passed,
            eval_gate_summary=summary or None,
        ),
        requires=PackRequires(redibis=default_compat_range(REDIBIS_VERSION)),
        contents=contents,
        mode="overlay",
    )
    sections: dict[str, Any] = {}
    for stem, doc in rule_docs.items():
        sections[f"text_gateway/rules/{stem}.yaml"] = doc
    for stem, doc in gaz_docs.items():
        sections[f"text_gateway/gazetteers/{stem}.yaml"] = doc
    for stem, doc in lex_docs.items():
        sections[f"text_gateway/lexicons/{stem}.yaml"] = doc

    readme_lines = [
        f"# {manifest.metadata.id}@{version}",
        "",
        description,
        "",
        f"uuid: {uid}",
        f"family_id: {family}",
        f"parent_uuid: {manifest.metadata.parent_uuid or ''}",
        f"eval_run_uuid: {eval_uid}",
        f"eval_gate_passed: {gate_passed}",
        f"eval_gate_summary: {summary}",
        "",
        "This version is immutable. Activate it separately into the live pack stack.",
        "",
    ]
    backend = store or default_pack_store()
    if dest is not None:
        out_dir = Path(dest)
        out_dir.mkdir(parents=True, exist_ok=True)
        archive = out_dir / f"{uid}.rdbpack"
        write_pack(
            archive,
            manifest,
            sections,
            readme="\n".join(readme_lines),
            check_registries=False,
        )
        return backend.publish(archive, author=str(author or "operator"))

    with tempfile.TemporaryDirectory(prefix="rdbpack-publish-") as td:
        archive = Path(td) / f"{uid}.rdbpack"
        write_pack(
            archive,
            manifest,
            sections,
            readme="\n".join(readme_lines),
            check_registries=False,
        )
        return backend.publish(archive, author=str(author or "operator"))


__all__ = [
    "PublishError",
    "default_pack_store",
    "default_pack_store_dir",
    "publish_text_gateway_pack",
]
