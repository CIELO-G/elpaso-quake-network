"""App-mode logic: watchdog restart policy and pipeline-control helpers."""

from __future__ import annotations

import io
import urllib.error

import pytest

from dashboard import appmode
from dashboard.appmode import MAX_RESTARTS, decide_restart


class TestDecideRestart:
    def test_running_pipeline_never_restarted(self):
        assert decide_restart(running=True, user_stopped=False, consecutive_restarts=0) is False

    def test_crashed_pipeline_restarts(self):
        assert decide_restart(running=False, user_stopped=False, consecutive_restarts=0) is True

    def test_user_stop_is_respected(self):
        # The whole point: Stop in the UI must not be fought by the watchdog.
        assert decide_restart(running=False, user_stopped=True, consecutive_restarts=0) is False

    def test_restart_budget_exhausts(self):
        assert decide_restart(False, False, MAX_RESTARTS - 1) is True
        assert decide_restart(False, False, MAX_RESTARTS) is False
        assert decide_restart(False, False, MAX_RESTARTS + 3) is False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"{}"))


class TestStartPipeline:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(appmode, "_api", lambda *a, **k: {"started": True})
        assert appmode.start_pipeline(8000) is True

    def test_already_running_is_success(self, monkeypatch):
        def raise409(*a, **k):
            raise _http_error(409)

        monkeypatch.setattr(appmode, "_api", raise409)
        assert appmode.start_pipeline(8000) is True

    def test_server_error_is_failure(self, monkeypatch):
        def raise500(*a, **k):
            raise _http_error(500)

        monkeypatch.setattr(appmode, "_api", raise500)
        assert appmode.start_pipeline(8000) is False

    def test_unreachable_server_is_failure(self, monkeypatch):
        def raise_url(*a, **k):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(appmode, "_api", raise_url)
        assert appmode.start_pipeline(8000) is False


class TestStopPipeline:
    def test_nothing_running_is_fine(self, monkeypatch):
        def raise404(*a, **k):
            raise _http_error(404)

        monkeypatch.setattr(appmode, "_api", raise404)
        appmode.stop_pipeline(8000)  # must not raise

    def test_waits_until_stopped(self, monkeypatch):
        calls = {"n": 0}

        def fake_api(port, path, method="GET", timeout=5.0):
            if path == "/api/pipeline/stop":
                return {"stopped": True}
            calls["n"] += 1
            return {"running": calls["n"] < 3, "pid": 1, "user_stopped": True}

        monkeypatch.setattr(appmode, "_api", fake_api)
        monkeypatch.setattr(appmode.time, "sleep", lambda s: None)
        appmode.stop_pipeline(8000, wait_s=10.0)
        assert calls["n"] == 3  # polled until running went False


class TestServerAlive:
    def test_alive(self, monkeypatch):
        monkeypatch.setattr(appmode, "_api", lambda *a, **k: {"ok": True})
        assert appmode.server_alive(8000) is True

    def test_dead(self, monkeypatch):
        def raise_url(*a, **k):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(appmode, "_api", raise_url)
        assert appmode.server_alive(8000) is False
