"""
redibis.behavior.store
=======================
Simple local-filesystem policy store.

Policies are immutable once saved (id + version + SHA are the identity).
An "active" pointer identifies the current version for each policy ID.

Storage layout:
    {base_dir}/
        {policy_id}/
            {version}.json          # compiled policy + document + lifecycle
            active.json             # pointer: {id, version, sha256}
            history.jsonl           # activation/approve/rollback events
            activation_stack.json   # prior active pointers for rollback
            simulations/{sim_id}.json  # durable simulation receipts
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from redibis.behavior.ids import validate_policy_id, validate_policy_version
from redibis.behavior.models import BehaviorPolicy


_SCHEMA_VERSION = "1"


class BehaviorPolicyStore:
    """Local filesystem-backed policy store."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _policy_dir(self, policy_id: str) -> Path:
        safe = validate_policy_id(policy_id)
        path = (self._base / safe).resolve()
        base = self._base.resolve()
        if path != base and base not in path.parents:
            raise ValueError(f"policy path escapes store base: {policy_id!r}")
        return self._base / safe

    def _version_path(self, policy_id: str, version: str) -> Path:
        safe_ver = validate_policy_version(version)
        return self._policy_dir(policy_id) / f"{safe_ver}.json"

    # ------------------------------------------------------------------
    # Save

    def save(
        self,
        policy: BehaviorPolicy,
        *,
        document: Optional[dict] = None,
        lifecycle: Optional[dict] = None,
    ) -> Path:
        """
        Persist a compiled policy. Raises ValueError if the
        (id, version, sha256) record already exists with a different SHA.
        """
        policy_dir = self._policy_dir(policy.id)
        policy_dir.mkdir(parents=True, exist_ok=True)
        dest = self._version_path(policy.id, policy.version)

        if dest.exists():
            existing = json.loads(dest.read_text(encoding="utf-8"))
            if existing.get("content_sha256") != policy.content_sha256:
                raise ValueError(
                    f"Policy ({policy.id}, {policy.version}) already exists with a "
                    "different SHA — create a new version instead of replacing."
                )
            return dest   # idempotent

        payload = _policy_to_dict(policy)
        if document is not None:
            payload["document"] = document
        if lifecycle is not None:
            payload["lifecycle"] = lifecycle
        dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return dest

    # ------------------------------------------------------------------
    # Activate / deactivate / rollback

    def activate(self, policy_id: str, version: str) -> None:
        """Set a version as active for a policy ID (pushes prior onto stack)."""
        candidate = self._version_path(policy_id, version)
        if not candidate.exists():
            raise FileNotFoundError(
                f"Policy ({policy_id}, {version}) not found — save it first."
            )
        data = json.loads(candidate.read_text(encoding="utf-8"))
        pointer = {
            "id": validate_policy_id(policy_id),
            "version": validate_policy_version(version),
            "sha256": data["content_sha256"],
        }
        policy_dir = self._policy_dir(policy_id)
        active_path = policy_dir / "active.json"
        if active_path.exists():
            prior = json.loads(active_path.read_text(encoding="utf-8"))
            history_path = policy_dir / "activation_stack.json"
            stack: list = []
            if history_path.exists():
                stack = json.loads(history_path.read_text(encoding="utf-8"))
            stack.append(prior)
            history_path.write_text(json.dumps(stack, indent=2), encoding="utf-8")
        active_path.write_text(json.dumps(pointer, indent=2), encoding="utf-8")

    def rollback_to_previous(self, policy_id: str) -> dict:
        """
        Pop the prior active pointer and restore it.

        Unlike ``activate``, this does not push the current pointer back onto
        the stack (so a second rollback continues backward, not forward).
        """
        policy_dir = self._policy_dir(policy_id)
        stack_path = policy_dir / "activation_stack.json"
        if not stack_path.exists():
            raise FileNotFoundError(f"no activation stack for {policy_id!r}")
        stack = json.loads(stack_path.read_text(encoding="utf-8"))
        if not stack:
            raise FileNotFoundError(f"no prior version to roll back to for {policy_id!r}")
        prior = stack.pop()
        stack_path.write_text(json.dumps(stack, indent=2), encoding="utf-8")
        version = prior.get("version")
        if not version:
            raise FileNotFoundError(f"corrupt activation stack for {policy_id!r}")
        # Point active at prior without re-pushing current
        candidate = self._version_path(policy_id, version)
        if not candidate.exists():
            raise FileNotFoundError(
                f"Policy ({policy_id}, {version}) not found during rollback"
            )
        data = json.loads(candidate.read_text(encoding="utf-8"))
        pointer = {
            "id": validate_policy_id(policy_id),
            "version": validate_policy_version(str(version)),
            "sha256": data["content_sha256"],
        }
        (policy_dir / "active.json").write_text(
            json.dumps(pointer, indent=2), encoding="utf-8"
        )
        return pointer

    def deactivate(self, policy_id: str) -> None:
        """Remove the active pointer (policy remains stored but inactive)."""
        active_file = self._policy_dir(policy_id) / "active.json"
        if active_file.exists():
            active_file.unlink()

    def get_active_pointer(self, policy_id: str) -> Optional[dict]:
        path = self._policy_dir(policy_id) / "active.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def previous_active_version(self, policy_id: str) -> Optional[str]:
        stack_path = self._policy_dir(policy_id) / "activation_stack.json"
        if not stack_path.exists():
            return None
        stack = json.loads(stack_path.read_text(encoding="utf-8"))
        if not stack:
            return None
        prior = stack[-1]
        return prior.get("version")

    # ------------------------------------------------------------------
    # Lifecycle metadata

    def set_lifecycle(self, policy_id: str, version: str, lifecycle: dict) -> None:
        path = self._version_path(policy_id, version)
        if not path.exists():
            raise FileNotFoundError(f"Policy ({policy_id}, {version}) not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["lifecycle"] = lifecycle
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def record_simulation(
        self,
        policy_id: str,
        version: str,
        simulation_id: str,
        aggregate: dict,
        *,
        content_sha256: str = "",
    ) -> None:
        path = self._version_path(policy_id, version)
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        lc = dict(data.get("lifecycle") or {})
        lc["last_simulation_id"] = simulation_id
        lc["last_simulation_aggregate"] = {
            k: v for k, v in aggregate.items() if k != "columns"
        }
        data["lifecycle"] = lc
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Durable receipt so approve/activate can validate across process restarts
        sim_id = str(simulation_id or "").strip()
        if sim_id and ".." not in sim_id and "/" not in sim_id and "\\" not in sim_id:
            sim_dir = self._policy_dir(policy_id) / "simulations"
            sim_dir.mkdir(parents=True, exist_ok=True)
            receipt = {
                "simulation_id": sim_id,
                "policy_id": validate_policy_id(policy_id),
                "version": validate_policy_version(version),
                "content_sha256": content_sha256 or data.get("content_sha256") or "",
                "aggregate": lc["last_simulation_aggregate"],
            }
            (sim_dir / f"{sim_id}.json").write_text(
                json.dumps(receipt, indent=2), encoding="utf-8"
            )

    def get_simulation_receipt(
        self, policy_id: str, simulation_id: str
    ) -> Optional[dict]:
        sim_id = str(simulation_id or "").strip()
        if not sim_id or ".." in sim_id or "/" in sim_id or "\\" in sim_id:
            return None
        path = self._policy_dir(policy_id) / "simulations" / f"{sim_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def append_lifecycle_event(self, policy_id: str, event: dict[str, Any]) -> None:
        policy_dir = self._policy_dir(policy_id)
        policy_dir.mkdir(parents=True, exist_ok=True)
        hist = policy_dir / "history.jsonl"
        with hist.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")

    def list_lifecycle_events(self, policy_id: str) -> list[dict]:
        hist = self._policy_dir(policy_id) / "history.jsonl"
        if not hist.exists():
            return []
        out: list[dict] = []
        for line in hist.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    # ------------------------------------------------------------------
    # Read

    def get(self, policy_id: str, version: str) -> Optional[dict]:
        """Return raw stored policy dict or None if not found."""
        path = self._version_path(policy_id, version)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def get_active(self, policy_id: str) -> Optional[dict]:
        """Return the active version's dict, or None."""
        pointer = self.get_active_pointer(policy_id)
        if pointer is None:
            return None
        return self.get(policy_id, pointer["version"])

    def list_active_policies(
        self,
        *,
        engine: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> list[dict]:
        """Return full stored records for every currently active policy."""
        out: list[dict] = []
        for item in self.list_policies():
            if not item.get("active"):
                continue
            if engine and item.get("engine") != engine:
                continue
            if stage and item.get("stage") != stage:
                continue
            data = self.get(item["id"], item["version"])
            if data is not None:
                out.append(data)
        return out

    def list_policies(self) -> list[dict]:
        """List all stored policies with id/version/active status."""
        result: list[dict] = []
        if not self._base.exists():
            return result
        for policy_dir in sorted(self._base.iterdir()):
            if not policy_dir.is_dir():
                continue
            policy_id = policy_dir.name
            try:
                validate_policy_id(policy_id)
            except ValueError:
                continue
            active_version: Optional[str] = None
            ptr = self.get_active_pointer(policy_id)
            if ptr:
                active_version = ptr.get("version")

            for f in sorted(policy_dir.glob("*.json")):
                if f.name in ("active.json", "activation_stack.json"):
                    continue
                version = f.stem
                try:
                    validate_policy_version(version)
                except ValueError:
                    continue
                data = json.loads(f.read_text(encoding="utf-8"))
                lc = data.get("lifecycle") or {}
                result.append({
                    "id": policy_id,
                    "version": version,
                    "active": version == active_version,
                    "content_sha256": data.get("content_sha256", ""),
                    "status": lc.get("status") or ("active" if version == active_version else "draft"),
                    "approved": bool(lc.get("approved")),
                    "engine": data.get("engine"),
                    "stage": data.get("stage"),
                    "description": data.get("description", ""),
                })
        return result


def _policy_to_dict(policy: BehaviorPolicy) -> dict:
    """Serialize a BehaviorPolicy to a JSON-safe dict."""
    return {
        "_schema_version": _SCHEMA_VERSION,
        "id": policy.id,
        "version": policy.version,
        "description": policy.description,
        "engine": policy.engine,
        "stage": policy.stage.value,
        "owner": policy.owner,
        "source": policy.source.value,
        "content_sha256": policy.content_sha256,
        "scope": {
            "engines": list(policy.scope.engines),
            "tables": list(policy.scope.tables),
            "columns": list(policy.scope.columns),
            "environments": list(policy.scope.environments),
            "jurisdictions": list(policy.scope.jurisdictions),
            "tenant": policy.scope.tenant,
            "effective_from": policy.scope.effective_from,
            "effective_until": policy.scope.effective_until,
        },
        "rules": [
            {
                "id": r.id,
                "reason": r.reason,
                "priority": r.priority,
                "terminal": r.terminal,
            }
            for r in policy.rules
        ],
    }
