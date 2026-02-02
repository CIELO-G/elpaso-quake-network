"""Entry point: python -m dashboard [--port PORT] [--browser]"""

import argparse
import threading

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(description="Seismic pipeline dashboard")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--browser", action="store_true",
                   help="Open in default browser instead of native window")
    args = p.parse_args()

    url = f"http://127.0.0.1:{args.port}"

    if args.browser:
        import webbrowser
        # start uvicorn, then open browser
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        uvicorn.run("dashboard.app:app", host="127.0.0.1", port=args.port,
                    log_level="warning")
    else:
        # start uvicorn in background thread, open native window in main thread
        server = threading.Thread(
            target=uvicorn.run,
            kwargs=dict(app="dashboard.app:app", host="127.0.0.1",
                        port=args.port, log_level="warning"),
            daemon=True,
        )
        server.start()

        import webview
        webview.create_window("El Paso Seismic Pipeline", url,
                              width=1280, height=800)
        webview.start()


if __name__ == "__main__":
    main()
