"""PiiGuardService — compose ScanBackend + MaskBackend behind one API."""

from __future__ import annotations

from typing import Any, Optional

from redibis.masking.engine import RunKeys
from redibis.pii.deid.applier import DeidResult
from redibis.pii.deid.policy import DeidPolicy
from redibis.pii.scan.result import DetectionResult

from redibis_pii_guard.mask_backend import MaskBackend
from redibis_pii_guard.pack_runtime import PackRuntime
from redibis_pii_guard.scan_backend import ScanBackend


class PiiGuardService:
    """Thin composer — no crypto or rule ownership of its own."""

    def __init__(
        self,
        *,
        pack: Optional[PackRuntime] = None,
        scan_backend: Optional[ScanBackend] = None,
        mask_backend: Optional[MaskBackend] = None,
    ):
        self.pack = pack or PackRuntime()
        self.scan_backend = scan_backend or ScanBackend(self.pack.ruleset)
        self.mask_backend = mask_backend or MaskBackend(self.pack.policies)
        self._sync_from_pack()

    def _sync_from_pack(self) -> None:
        self.pack.reload()
        self.scan_backend.replace_ruleset(self.pack.ruleset)
        self.mask_backend.replace_policies(self.pack.policies)

    def health(self) -> dict[str, Any]:
        self._sync_from_pack()
        return {
            "ok": True,
            "service": "redibis-pii-guard",
            "ruleset_id": self.pack.ruleset.id,
            "ruleset_version": self.pack.ruleset.version,
            "policies": len(self.pack.policies),
            "pack_path": self.pack.pack_path or None,
        }

    def ruleset(self) -> dict[str, Any]:
        self._sync_from_pack()
        return self.pack.ruleset_info()

    def policies(self) -> dict[str, Any]:
        self._sync_from_pack()
        return {"policies": self.mask_backend.list_policies()}

    def scan(
        self,
        *,
        kind: str = "span",
        text: Optional[str] = None,
        records: Any = None,
        **kwargs: Any,
    ) -> DetectionResult:
        self._sync_from_pack()
        if kind == "column":
            if records is None:
                raise ValueError("records required for kind=column")
            return self.scan_backend.scan_columns(records, **kwargs)
        if text is None:
            raise ValueError("text required for kind=span")
        return self.scan_backend.scan_text(text, **kwargs)

    def deidentify(
        self,
        *,
        kind: str = "span",
        text: Optional[str] = None,
        records: Any = None,
        detections: Optional[list[dict[str, Any]]] = None,
        result: Optional[DetectionResult] = None,
        policy_id: Optional[str] = None,
        policy: Optional[dict[str, Any] | DeidPolicy] = None,
        only_detected: bool = True,
        seed: Optional[str] = None,
    ) -> DeidResult:
        self._sync_from_pack()
        pol = self.mask_backend.resolve_policy(policy_id=policy_id, policy=policy)
        keys = RunKeys.mint(seed=seed) if seed else RunKeys.mint()
        if result is None:
            if not detections:
                raise ValueError("detections or result required for deidentify")
            result = self.mask_backend.detections_from_payload(
                detections,
                kind=kind,
                ruleset_id=self.pack.ruleset.id,
                ruleset_version=self.pack.ruleset.version,
            )
        if kind == "column":
            if records is None:
                raise ValueError("records required for kind=column deidentify")
            return self.mask_backend.deidentify_columns(
                records, result, pol, keys=keys, only_detected=only_detected
            )
        if text is None:
            raise ValueError("text required for kind=span deidentify")
        return self.mask_backend.deidentify_text(text, result, pol, keys=keys)

    def scan_and_mask(
        self,
        *,
        kind: str = "span",
        text: Optional[str] = None,
        records: Any = None,
        policy_id: Optional[str] = None,
        policy: Optional[dict[str, Any] | DeidPolicy] = None,
        scan_kwargs: Optional[dict[str, Any]] = None,
        only_detected: bool = True,
        seed: Optional[str] = None,
    ) -> tuple[DetectionResult, DeidResult]:
        sk = dict(scan_kwargs or {})
        result = self.scan(kind=kind, text=text, records=records, **sk)
        deid = self.deidentify(
            kind=kind,
            text=text,
            records=records,
            result=result,
            policy_id=policy_id,
            policy=policy,
            only_detected=only_detected,
            seed=seed,
        )
        return result, deid
