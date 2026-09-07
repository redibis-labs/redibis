"""Pack version identity: UUID, family lineage, stack hash, contents summary.

A UUID identifies an immutable pack *version*, not a pack family. Every publish
mints a new UUID (uuid4 when stamped into a new manifest; uuid5 back-fill for
builtins that shipped without one). A UUID is never reused or re-pointed.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, Union

from redibis.pack.canonical import parse_yaml_bytes, sha256_bytes
from redibis.pack.errors import PackValidationError
from redibis.pack.models import PackContents, PackManifest

# Fixed namespaces — never change. Air-gapped installs of the same redibis
# release must agree on builtin pack UUIDs and on stack_uuid for a given stack.
_PACK_NS = uuid.UUID("6f1e0d9c-3a52-4b78-9c41-0e2d7f5a8b63")
_STACK_NS = uuid.UUID("2c9b8a17-4d63-4e05-b1f2-7a3e6d0c4519")

CONTENTS_SUMMARY_KEYS: tuple[str, ...] = (
    "regex_patterns",
    "custom_rules",
    "ner_entities",
    "validators",
    "behavior_rules",
    "quality_rules",
    "masking_rules",
)


class HasUuidSha256(Protocol):
    """Minimal surface for ``compute_stack_uuid`` (PackRef / layer / SimpleNamespace)."""

    uuid: str
    sha256: str


def backfill_uuid(family_id: str, version: str, pack_sha256: str) -> str:
    """Deterministic UUID for a builtin / pre-identity pack version.

    Two air-gapped installs of the same redibis release resolve the same
    builtin pack to the same UUID.
    """
    return str(uuid.uuid5(_PACK_NS, f"{family_id}|{version}|{pack_sha256}"))


def mint_pack_uuid() -> str:
    """Mint a new UUID for a freshly published pack version (never reuse)."""
    return str(uuid.uuid4())


def infer_pack_kind(contents: PackContents, *, pack_id: str = "") -> str:
    """Best-effort kind for ``PackRef.kind`` / evidence ``pack_stack``."""
    from redibis.pack.defaults import DEFAULT_PACK_ID

    if pack_id == DEFAULT_PACK_ID:
        return "default"
    if contents.classification:
        return "policy"
    if contents.behavior:
        return "behavior"
    if contents.locale and not contents.config:
        return "locale"
    if contents.quality or contents.masking:
        return "policy"
    if contents.ner:
        return "ner"
    if contents.config:
        return "config"
    return "pack"


def infer_family_id(*, author: str, kind: str, pack_id: str) -> str:
    """Stable family id: ``{org}.{kind}.{id}`` (builtins under ``redibis.builtin``)."""
    org = (author or "").strip() or "unknown"
    pid = (pack_id or "").strip() or "unnamed"
    k = (kind or "pack").strip() or "pack"
    if org.lower() == "redibis":
        if k == "default":
            return f"redibis.builtin.{pid}"
        return f"redibis.builtin.{k}.{pid}"
    return f"{org}.{k}.{pid}"


def ensure_pack_identity(manifest: PackManifest, pack_sha256: str) -> PackManifest:
    """Fill ``uuid`` / ``family_id`` when a pre-identity manifest omitted them.

    Mutates ``manifest.metadata`` in place and returns ``manifest``.
    """
    md = manifest.metadata
    kind = infer_pack_kind(manifest.contents, pack_id=md.id)
    family_id = (md.family_id or "").strip() or infer_family_id(
        author=md.author or "",
        kind=kind,
        pack_id=md.id,
    )
    uuid_value = (md.uuid or "").strip() or backfill_uuid(
        family_id, md.version, pack_sha256
    )
    md.family_id = family_id
    md.uuid = uuid_value
    return manifest


def _require_stack_identity(
    ordered_packs: Sequence[HasUuidSha256],
) -> list[tuple[str, str]]:
    """Fail closed: every layer must carry a real uuid and a content digest.

    Rendering ``None`` / ``""`` into the payload used to mint a confident-looking
    ``stack_uuid`` for a pack that has no identity. Replay then cites a stack
    that cannot be recovered.
    """
    pairs: list[tuple[str, str]] = []
    for i, pack in enumerate(ordered_packs):
        raw_uuid = getattr(pack, "uuid", None)
        raw_sha = getattr(pack, "sha256", None)
        uuid_text = raw_uuid.strip() if isinstance(raw_uuid, str) else ""
        sha_text = raw_sha.strip() if isinstance(raw_sha, str) else ""
        if not uuid_text:
            raise ValueError(
                f"compute_stack_uuid: pack at index {i} has no uuid "
                f"(got {raw_uuid!r}); refuse to mint a stack identity"
            )
        if not sha_text:
            raise ValueError(
                f"compute_stack_uuid: pack at index {i} has no sha256 "
                f"(got {raw_sha!r}); refuse to mint a stack identity"
            )
        try:
            uuid.UUID(uuid_text)
        except ValueError as exc:
            raise ValueError(
                f"compute_stack_uuid: pack at index {i} uuid is not a UUID: "
                f"{uuid_text!r}"
            ) from exc
        pairs.append((uuid_text, sha_text))
    return pairs


def _stack_payload(ordered_packs: Sequence[HasUuidSha256], *, mode: str) -> str:
    """Canonical identity string: ordered ``uuid:sha256`` plus overlay mode."""
    pairs = _require_stack_identity(ordered_packs)
    body = "|".join(f"{u}:{s}" for u, s in pairs)
    return f"{body}|mode={mode}"


def compute_stack_uuid(ordered_packs: Sequence[HasUuidSha256], *, mode: str) -> str:
    """Deterministic UUID of an ordered overlay stack (order is part of identity).

    Raises ``ValueError`` if any pack is missing ``uuid`` or ``sha256``. A
    missing identity must not produce a stack UUID.
    """
    return str(uuid.uuid5(_STACK_NS, _stack_payload(ordered_packs, mode=mode)))


def compute_stack_sha256(ordered_packs: Sequence[HasUuidSha256], *, mode: str) -> str:
    """SHA-256 of the ordered ``uuid:sha256`` list plus mode.

    Includes each pack's content digest so tampering a blob changes the stack
    hash even when UUIDs stay the same. UUID answers *which*; sha256 answers
    *whether it is intact* — both belong in the stack fingerprint.
    """
    return sha256_bytes(_stack_payload(ordered_packs, mode=mode).encode("utf-8"))


def _yaml_or_json(data: bytes, relpath: str) -> Any:
    lower = relpath.lower()
    if lower.endswith(".json"):
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    try:
        return parse_yaml_bytes(data)
    except Exception:
        return None


def count_pack_contents(
    files: Mapping[str, bytes],
    manifest: PackManifest | None = None,
) -> dict[str, int]:
    """Operator-facing section counts stored in ``index.json`` (no blob fetch)."""
    counts = {k: 0 for k in CONTENTS_SUMMARY_KEYS}

    regex = files.get("locale/regex.yaml") or files.get("locale/regex.yml")
    if regex:
        doc = _yaml_or_json(regex, "locale/regex.yaml")
        if isinstance(doc, dict):
            add = doc.get("add")
            if isinstance(add, dict):
                counts["regex_patterns"] = len(add)
            elif isinstance(add, list):
                counts["regex_patterns"] = len(add)

    ner = files.get("ner/models.yaml") or files.get("ner/models.yml")
    if ner:
        doc = _yaml_or_json(ner, "ner/models.yaml")
        if isinstance(doc, dict):
            labels = doc.get("labels")
            phrases = doc.get("phrases")
            if isinstance(labels, list) and labels:
                counts["ner_entities"] = len(labels)
            elif isinstance(phrases, dict):
                counts["ner_entities"] = len(phrases)

    if manifest is not None:
        counts["validators"] = len(manifest.requires.registries.validators)

    for rel, data in files.items():
        if rel.startswith("classification/packs/") and rel.endswith((".yaml", ".yml")):
            doc = _yaml_or_json(data, rel)
            if isinstance(doc, dict):
                rules = doc.get("edge_rules") or []
                if isinstance(rules, list):
                    counts["custom_rules"] += len(rules)
        if rel.startswith("behavior/") and rel.endswith((".yaml", ".yml")):
            doc = _yaml_or_json(data, rel)
            if isinstance(doc, dict):
                rules = doc.get("rules") or []
                if isinstance(rules, list):
                    counts["behavior_rules"] += len(rules)
        if rel.startswith("quality/rulesets/") and rel.endswith((".yaml", ".yml")):
            doc = _yaml_or_json(data, rel)
            if isinstance(doc, dict):
                exp = doc.get("expectations") or doc.get("rules") or []
                if isinstance(exp, list):
                    counts["quality_rules"] += len(exp)
        if rel.startswith("masking/plans/") and rel.endswith((".yaml", ".yml")):
            doc = _yaml_or_json(data, rel)
            if isinstance(doc, dict):
                cols = doc.get("columns") or []
                if isinstance(cols, list):
                    counts["masking_rules"] += len(cols)

    return counts


def load_trust_store(path: Union[str, Path, None]) -> dict[str, str]:
    """Load ``key_id → public key`` (base64 or hex) from JSON or YAML."""
    if not path:
        return {}
    target = Path(path).expanduser()
    if not target.is_file():
        return {}
    raw = target.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        import yaml

        doc = yaml.safe_load(raw)
    if not isinstance(doc, dict):
        return {}
    keys = doc.get("keys") if isinstance(doc.get("keys"), dict) else doc
    out: dict[str, str] = {}
    for key_id, value in keys.items():
        if isinstance(value, dict):
            value = value.get("public_key") or value.get("key") or ""
        text = str(value or "").strip()
        if key_id and text:
            out[str(key_id)] = text
    return out


def _decode_key_bytes(value: str) -> bytes:
    text = value.strip()
    if text.startswith("hex:"):
        return bytes.fromhex(text[4:].strip())
    if all(c in "0123456789abcdefABCDEF" for c in text.replace(" ", "")) and len(text.replace(" ", "")) == 64:
        return bytes.fromhex(text.replace(" ", ""))
    try:
        return base64.b64decode(text)
    except Exception as exc:
        raise ValueError(f"unreadable public key: {exc}") from exc


def verify_pack_signature(
    manifest: PackManifest,
    *,
    pack_sha256: str,
    trust_store: Mapping[str, str] | None = None,
    require: bool = False,
) -> Optional[dict[str, Any]]:
    """Verify ``PackManifest.signature`` when present.

    An unverifiable signature is not a hard failure by default (air-gapped
    sites may lack the key). Surfaces as ``verified: false`` with a reason.
    ``require=True`` (config ``pack.require_signature``) raises instead.
    """
    sig = manifest.signature
    if not sig:
        if require:
            raise PackValidationError(
                "pack.require_signature is true but pack has no signature",
                errors=["missing signature"],
            )
        return None
    if not isinstance(sig, dict):
        result = {
            "algo": None,
            "key_id": None,
            "verified": False,
            "reason": "invalid signature block",
        }
        if require:
            raise PackValidationError(
                "pack signature is not a mapping",
                errors=[str(result["reason"])],
            )
        return result

    algo = str(sig.get("algo") or sig.get("algorithm") or "ed25519").strip() or "ed25519"
    key_id = str(sig.get("key_id") or sig.get("keyId") or "").strip()
    value = str(sig.get("value") or sig.get("signature") or "").strip()
    trust = dict(trust_store or {})

    if not key_id or key_id not in trust:
        result = {
            "algo": algo,
            "key_id": key_id or None,
            "verified": False,
            "reason": "key_id not in trust store",
        }
        if require:
            raise PackValidationError(
                "pack signature key is not in the trust store",
                errors=[str(result["reason"])],
            )
        return result

    if algo.lower() != "ed25519":
        result = {
            "algo": algo,
            "key_id": key_id,
            "verified": False,
            "reason": f"unsupported signature algo: {algo}",
        }
        if require:
            raise PackValidationError(
                f"unsupported pack signature algorithm: {algo}",
                errors=[str(result["reason"])],
            )
        return result

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        result = {
            "algo": algo,
            "key_id": key_id,
            "verified": False,
            "reason": "cryptography not installed",
        }
        if require:
            raise PackValidationError(
                "cannot verify pack signature: cryptography not installed",
                errors=[str(result["reason"])],
            )
        return result

    try:
        pub = Ed25519PublicKey.from_public_bytes(_decode_key_bytes(trust[key_id]))
        pub.verify(base64.b64decode(value), bytes.fromhex(pack_sha256))
    except InvalidSignature:
        result = {
            "algo": algo,
            "key_id": key_id,
            "verified": False,
            "reason": "signature mismatch",
        }
        if require:
            raise PackValidationError(
                "pack signature does not match pack digest",
                errors=[str(result["reason"])],
            )
        return result
    except Exception as exc:
        result = {
            "algo": algo,
            "key_id": key_id,
            "verified": False,
            "reason": f"verification failed: {exc}",
        }
        if require:
            raise PackValidationError(
                "pack signature verification failed",
                errors=[str(result["reason"])],
            )
        return result

    return {"algo": algo, "key_id": key_id, "verified": True}
