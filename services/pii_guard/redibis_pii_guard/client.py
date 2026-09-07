"""HTTP client for the PII Guard service."""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class PiiGuardClient:
    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        timeout: float = 60.0,
    ):
        self.base_url = (
            base_url or os.environ.get("REDIBIS_PII_GUARD_URL") or "http://127.0.0.1:8090"
        ).rstrip("/")
        self.api_key = (api_key or os.environ.get("REDIBIS_PII_GUARD_API_KEY") or "").strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/health")
            r.raise_for_status()
            return r.json()

    def scan(self, **body: Any) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{self.base_url}/scan", json=body, headers=self._headers())
            r.raise_for_status()
            return r.json()

    def deidentify(self, **body: Any) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/deidentify", json=body, headers=self._headers()
            )
            r.raise_for_status()
            return r.json()

    def scan_and_mask(self, **body: Any) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/scan-and-mask", json=body, headers=self._headers()
            )
            r.raise_for_status()
            return r.json()

    def policies(self) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/policies", headers=self._headers())
            r.raise_for_status()
            return r.json()

    def ruleset(self) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/ruleset", headers=self._headers())
            r.raise_for_status()
            return r.json()
