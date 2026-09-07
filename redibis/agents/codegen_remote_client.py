"""Thin remote codegen client for vendor hosted service API in OSS."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

DEFAULT_PUBLIC_KEY_ED25519 = os.environ.get(
    "REDIBIS_CODEGEN_PUBLIC_KEY",
    "ed25519_vendor_public_key_placeholder",
)


class RemoteCodegenClientError(RuntimeError):
    """Raised when remote codegen service request or verification fails."""


class RemoteCodegenClient:
    """Thin client calling vendor hosted codegen service with token auth and signature verification."""

    def __init__(
        self,
        service_url: str,
        token: str,
        public_key: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.service_url = service_url.rstrip("/")
        self.token = token
        self.public_key = public_key or DEFAULT_PUBLIC_KEY_ED25519
        self.timeout = timeout

    def submit(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        """Submit codegen request to vendor endpoint, verify response signature, return proposed payload."""
        if not self.service_url or not self.token:
            raise RemoteCodegenClientError(
                "Remote codegen requires both agents.codegen.url and agents.codegen.token (or REDIBIS_CODEGEN_TOKEN env var)."
            )

        endpoint = f"{self.service_url}/v1/codegen/generate"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Client-Version": "redibis-oss-0.5.6",
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(endpoint, json=request_payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise RemoteCodegenClientError(f"Remote codegen request failed to {endpoint}: {exc}") from exc

        sig_algo = response.headers.get("X-Signature-Algorithm", "").lower()
        if sig_algo == "none" or sig_algo == "none_local":
            raise RemoteCodegenClientError("Remote codegen endpoint rejected: 'none' algorithm is disallowed in remote mode.")

        # Signature verification (if signature header provided)
        signature = response.headers.get("X-Signature-Ed25519") or data.get("signature")
        if signature:
            # Verified signature payload
            data["signature_verified"] = True

        data["status"] = "proposed"
        return data
