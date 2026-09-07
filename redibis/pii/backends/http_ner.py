"""Optional HTTP NER adapter — remote inference when explicitly configured."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from redibis.pii.ner_backend import (
    DEFAULT_NER_LABELS,
    BaseNERBackend,
    NERHit,
    NERReport,
    NERSpan,
)

logger = logging.getLogger("pii.ner.http")


@dataclass
class RemoteNERBackend(BaseNERBackend):
    """
    POST ``{values, labels, column}`` to a configured URL; expects JSON hits.

    Only used when ``type: http`` appears in a model manifest or spec.
    """

    endpoint_url: str
    labels: list[str] = field(default_factory=lambda: list(DEFAULT_NER_LABELS))
    timeout_sec: float = 30.0
    api_key: str = ""

    @property
    def name(self) -> str:
        return f"http:{self.endpoint_url}"

    def analyze(
        self,
        values: list[str],
        column_name: str,
        *,
        labels: list[str] | None = None,
    ) -> NERReport:
        active_labels = list(labels or self.labels)
        report = NERReport(model=self.name, labels_requested=active_labels)
        payload = {
            "column": column_name,
            "labels": active_labels,
            "values": values,
        }
        try:
            raw = self._post_json(payload)
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Remote NER request failed: %s", exc)
            return report

        hits_raw = raw.get("hits") if isinstance(raw, dict) else None
        if not isinstance(hits_raw, list):
            return report

        from redibis.pii.ner_backend import _normalize_ner_label

        total = max(len(values), 1)
        agg: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "best_score": 0.0, "display_label": ""}
        )
        for item in hits_raw:
            if not isinstance(item, dict):
                continue
            raw_label = _normalize_ner_label(str(item.get("label") or ""))
            if not raw_label:
                continue
            from redibis.models import canonical_entity

            key = canonical_entity(raw_label.upper().replace(" ", "_")) or raw_label
            score = float(item.get("score") or 0)
            bucket = agg[key]
            bucket["count"] += int(item.get("count") or 1)
            if score > bucket["best_score"]:
                bucket["best_score"] = score
                bucket["display_label"] = raw_label

        for bucket in agg.values():
            if bucket["best_score"] <= 0:
                continue
            report.hits.append(NERHit(
                label=bucket["display_label"],
                score=round(bucket["best_score"], 4),
                match_rate=round(bucket["count"] / total, 4),
            ))
        report.hits.sort(key=lambda h: h.score, reverse=True)
        return report

    def analyze_text(
        self,
        text: str,
        *,
        labels: list[str] | None = None,
        phrases: dict[str, str] | None = None,
    ) -> list[NERSpan]:
        """Request span-level NER from the remote endpoint when supported."""
        if not text:
            return []
        active_labels = list(labels or self.labels)
        payload = {
            "text": text,
            "labels": active_labels,
            "mode": "spans",
        }
        if phrases:
            payload["phrases"] = phrases
        try:
            raw = self._post_json(payload)
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Remote NER analyze_text failed: %s", exc)
            return []

        items = raw.get("spans") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []

        from redibis.pii.ner_backend import _normalize_ner_label

        spans: list[NERSpan] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_label = _normalize_ner_label(str(item.get("label") or ""))
            if not raw_label:
                continue
            try:
                start = int(item["start"])
                end = int(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if start < 0 or end > len(text) or start >= end:
                continue
            spans.append(NERSpan(
                start=start,
                end=end,
                label=raw_label,
                score=float(item.get("score") or 0),
                text=text[start:end],
                model=self.name,
            ))
        return spans

    def health_check(self) -> dict:
        report: dict[str, Any] = {
            "type": "http",
            "endpoint": self.endpoint_url,
            "loadable": False,
        }
        try:
            raw = self._post_json({"health": True, "labels": self.labels})
            report["loadable"] = bool(raw.get("ok", True))
        except Exception as exc:
            report["error"] = str(exc)
        return report

    def _post_json(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = Request(
            self.endpoint_url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=self.timeout_sec) as resp:
            data = resp.read().decode("utf-8")
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else {}
