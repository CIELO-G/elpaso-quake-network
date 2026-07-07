"""Entry point: ``python -m dashboard [--port PORT] [--browser] [--host HOST]``.

Default: native pywebview window bound to localhost. The menubar lives
in-window (see ``dashboard/static/index.html`` + ``app.js``) so it works
identically in the native window and a browser tab. ``--browser`` opens
the same page in the user's default browser instead of pywebview.

To expose the dashboard to other machines (e.g. over Tailscale), pass
``--host 0.0.0.0`` AND enable auth::

    export DASHBOARD_AUTH_ENABLED=1
    export DASHBOARD_PASSWORD='...'      # remote browsers prompt once
    python -m dashboard --host 0.0.0.0

The local pywebview window / loopback connections never need the password.
Binding beyond localhost without auth requires an explicit
``--allow-insecure`` (old behavior — every write endpoint open to the
network).
"""

from __future__ import annotations

import argparse
import sys
import threading

import uvicorn

APP_TITLE = "El Paso Seismic Pipeline"


def main() -> None:
    p = argparse.ArgumentParser(description="Seismic pipeline dashboard")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Default 127.0.0.1 (localhost only). "
        "Use 0.0.0.0 to expose to your LAN / Tailscale tailnet.",
    )
    p.add_argument(
        "--browser",
        action="store_true",
        help="Open in the default browser instead of the native window.",
    )
    p.add_argument(
        "--allow-insecure",
        action="store_true",
        help="Allow binding beyond localhost WITHOUT auth (not recommended).",
    )
    p.add_argument(
        "--app",
        action="store_true",
        help="Desktop-app mode: auto-start and supervise the pipeline; "
        "closing the window shuts everything down cleanly. "
        "(Used by El Paso Monitor.app — see deploy/app/.)",
    )
    p.add_argument(
        "--no-autostart",
        action="store_true",
        help="With --app: do not auto-start or supervise the pipeline.",
    )
    args = p.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_insecure:
        from dashboard.middleware import AUTH_ENABLED, AUTH_PASSWORD

        if not (AUTH_ENABLED and AUTH_PASSWORD):
            sys.exit(
                f"Refusing to bind to {args.host} without authentication: write "
                "endpoints (pipeline control, event save, admin) would be open "
                "to the network.\n"
                "Either enable auth:\n"
                "    export DASHBOARD_AUTH_ENABLED=1\n"
                "    export DASHBOARD_PASSWORD='...'\n"
                "or pass --allow-insecure to accept the risk."
            )

    if args.app:
        from dashboard.appmode import run_app

        run_app(args.port, host=args.host, autostart=not args.no_autostart)
        return

    # The local URL we open in the browser/window. Always use 127.0.0.1
    # for the client side even when binding to 0.0.0.0 — the user's own
    # browser still wants a loopback connection.
    url = f"http://127.0.0.1:{args.port}"

    if args.host != "127.0.0.1":
        print(
            f"[dashboard] Binding to {args.host}:{args.port} — accessible from other machines on this network."
        )

    if args.browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        uvicorn.run(
            "dashboard.app:app",
            host=args.host,
            port=args.port,
            log_level="warning",
        )
        return

    # Native pywebview window. Menubar is in-window (HTML/CSS/JS).
    server = threading.Thread(
        target=uvicorn.run,
        kwargs=dict(
            app="dashboard.app:app",
            host=args.host,
            port=args.port,
            log_level="warning",
        ),
        daemon=True,
    )
    server.start()

    import webview

    webview.create_window(APP_TITLE, url, width=1280, height=800)
    webview.start()


if __name__ == "__main__":
    main()
