"""App mode: run the monitor as a self-contained desktop application.

``python -m dashboard --app`` gives the "El Paso Monitor.app" behavior:

* if a monitor server is already running on the port, just open a window
  onto it (second launches are viewers — they don't start or stop anything);
* otherwise start the web server, auto-start the pipeline in continuous
  mode, and babysit it: a watchdog restarts the pipeline (with backoff) if
  it dies, but never fights an explicit Stop pressed in the UI;
* closing the window shuts everything down cleanly — pipeline first
  (SIGTERM → run_pipeline's graceful KeyboardInterrupt flow), then the app.

All pipeline control goes through the dashboard's own HTTP endpoints on
loopback (which bypass auth), so there is exactly one lifecycle mechanism
whether the operator clicks buttons or the app acts on its own.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("dashboard.app")

APP_TITLE = "El Paso Monitor"

# Watchdog tuning. Backoff doubles per consecutive restart; a pipeline that
# stays up for RESET_AFTER_S is considered healthy again. After MAX_RESTARTS
# consecutive failures the watchdog gives up (the UI status dot shows the
# pipeline stopped, and the log says why).
POLL_INTERVAL_S = 20.0
BACKOFF_START_S = 30.0
BACKOFF_CAP_S = 600.0
RESET_AFTER_S = 3600.0
MAX_RESTARTS = 6


def _api(port: int, path: str, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def server_alive(port: int) -> bool:
    try:
        _api(port, "/api/health", timeout=1.5)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_server_ready(port: int, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if server_alive(port):
            return True
        time.sleep(0.3)
    return False


def pipeline_state(port: int) -> dict:
    """{'running': bool, 'pid': int|None, 'user_stopped': bool}"""
    return _api(port, "/api/pipeline/running")


def start_pipeline(port: int) -> bool:
    """Start continuous mode; an already-running pipeline (409) counts as ok."""
    try:
        _api(port, "/api/pipeline/start", method="POST")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return True
        logger.error("Pipeline start failed: HTTP %s", exc.code)
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.error("Pipeline start failed: %s", exc)
        return False


def stop_pipeline(port: int, wait_s: float = 20.0) -> None:
    """Graceful stop; waits for the process to exit (bounded)."""
    try:
        _api(port, "/api/pipeline/stop", method="POST")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # nothing running
            return
        logger.warning("Pipeline stop returned HTTP %s", exc.code)
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Pipeline stop failed: %s", exc)
        return
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            if not pipeline_state(port)["running"]:
                return
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            return
        time.sleep(0.5)
    logger.warning("Pipeline still shutting down after %.0fs — leaving it to finish", wait_s)


def decide_restart(running: bool, user_stopped: bool, consecutive_restarts: int) -> bool:
    """The watchdog's whole policy, factored out for tests."""
    if running or user_stopped:
        return False
    return consecutive_restarts < MAX_RESTARTS


class PipelineWatchdog(threading.Thread):
    """Restarts a crashed pipeline; respects explicit user stops."""

    def __init__(self, port: int):
        super().__init__(name="pipeline-watchdog", daemon=True)
        self.port = port
        self._stop_event = threading.Event()
        self.consecutive_restarts = 0
        self._last_healthy = time.monotonic()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(POLL_INTERVAL_S):
            try:
                state = pipeline_state(self.port)
            except (urllib.error.URLError, OSError, ValueError):
                continue  # server hiccup — not the pipeline's problem
            if state.get("running"):
                if time.monotonic() - self._last_healthy > RESET_AFTER_S:
                    self.consecutive_restarts = 0
                self._last_healthy = time.monotonic()
                continue
            if not decide_restart(False, bool(state.get("user_stopped")),
                                  self.consecutive_restarts):
                if not state.get("user_stopped"):
                    logger.error(
                        "Pipeline down and restart budget exhausted (%d) — "
                        "not restarting. Check logs/pipeline_stdout.log",
                        self.consecutive_restarts,
                    )
                continue
            backoff = min(BACKOFF_START_S * (2 ** self.consecutive_restarts), BACKOFF_CAP_S)
            logger.warning(
                "Pipeline is down (not user-stopped) — restarting in %.0fs "
                "(attempt %d/%d)", backoff, self.consecutive_restarts + 1, MAX_RESTARTS,
            )
            if self._stop_event.wait(backoff):
                return
            if start_pipeline(self.port):
                self.consecutive_restarts += 1
                self._last_healthy = time.monotonic()


def run_app(port: int, host: str = "127.0.0.1", autostart: bool = True) -> None:
    """Entry point for --app mode. Blocks until the window closes."""
    import webview

    url = f"http://127.0.0.1:{port}"

    # Second launch? Attach a viewer window to the existing server and do
    # NOT take ownership of the pipeline lifecycle.
    if server_alive(port):
        logger.info("Monitor already running on port %d — opening a window onto it", port)
        webview.create_window(APP_TITLE, url, width=1280, height=800)
        webview.start()
        return

    import uvicorn

    server = threading.Thread(
        target=uvicorn.run,
        kwargs=dict(app="dashboard.app:app", host=host, port=port, log_level="warning"),
        daemon=True,
    )
    server.start()

    if not wait_server_ready(port):
        raise SystemExit(f"Dashboard server failed to start on port {port} within 30s")

    watchdog = None
    if autostart:
        if start_pipeline(port):
            logger.info("Pipeline running (continuous mode)")
        watchdog = PipelineWatchdog(port)
        watchdog.start()

    webview.create_window(APP_TITLE, url, width=1280, height=800)
    webview.start()  # blocks until the window is closed

    # ── Clean shutdown: window closed = quit the monitor ─────────────
    if watchdog is not None:
        watchdog.stop()
    if autostart:
        logger.info("Window closed — stopping the pipeline gracefully")
        stop_pipeline(port)
    # uvicorn runs in a daemon thread; process exit takes it down.
