"""Open-core reporting seam — stub when commercial add-on is absent."""

from __future__ import annotations

from fastapi import FastAPI

from redibis.reporting import ReportingAddonMissing


def test_reporting_available_false_without_addon(monkeypatch):
    from redibis import reporting as r

    monkeypatch.setattr(r, "get_reporting_plugin", lambda: None)
    assert r.reporting_available() is False
    assert r.register_reporting(FastAPI()) is None


def test_run_cli_without_addon_returns_hint(capsys, monkeypatch):
    from redibis import reporting as r

    monkeypatch.setattr(r, "get_reporting_plugin", lambda: None)

    class Args:
        report_action = "pii"

    code = r.run_reporting_cli(Args())
    assert code == 2
    err = capsys.readouterr().err
    assert "redibis-reports" in err


def test_addon_missing_message():
    exc = ReportingAddonMissing("detail")
    assert "redibis-reports" in str(exc)
    assert "detail" in str(exc)


def test_register_reporting_calls_plugin(monkeypatch):
    calls = {}

    class FakePlugin:
        name = "fake"
        version = "0"

        def register_web(self, app, **kwargs):
            calls["app"] = app
            return {"name": self.name}

        def run_cli(self, args):
            return 0

        def create_service(self, **kwargs):
            return None

    from redibis import reporting as r

    monkeypatch.setattr(r, "get_reporting_plugin", lambda: FakePlugin())
    app = FastAPI()
    meta = r.register_reporting(app)
    assert meta == {"name": "fake"}
    assert calls["app"] is app
