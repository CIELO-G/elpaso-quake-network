"""Entry point: ``python -m dashboard [--port PORT] [--browser] [--host HOST]``.

Default: native pywebview window bound to localhost. The menubar lives
in-window (see ``dashboard/static/index.html`` + ``app.js``) so it works
identically in the native window and a browser tab. ``--browser`` opens
the same page in the user's default browser instead of pywebview.

To expose the dashboard to other machines (e.g. over Tailscale), pass
``--host 0.0.0.0``. Only do this when the network is trusted — there is
no auth on write endpoints.
"""

from __future__ import annotations

import argparse
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
    args = p.parse_args()

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
