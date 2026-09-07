"""HTTP NER adapter tests (mocked endpoint)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from redibis.pii.backends.http_ner import RemoteNERBackend


def test_remote_ner_analyze_parses_hits():
    backend = RemoteNERBackend(
        endpoint_url="http://127.0.0.1:9999/ner",
        labels=["person"],
    )
    payload = {
        "hits": [
            {"label": "person", "score": 0.92, "count": 2},
            {"label": "phone number", "score": 0.71, "count": 1},
        ]
    }

    with patch("redibis.pii.backends.http_ner.urlopen") as mock_urlopen:
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        mock_urlopen.return_value = resp

        report = backend.analyze(["a", "b"], "col", labels=["person", "phone number"])

    assert len(report.hits) == 2
    assert report.best().label == "person"
    result = report.to_result()
    assert result["score"] == 0.92


def test_remote_ner_health_check():
    backend = RemoteNERBackend(endpoint_url="http://127.0.0.1:9999/health")
    with patch.object(backend, "_post_json", return_value={"ok": True}):
        health = backend.health_check()
    assert health["loadable"] is True
