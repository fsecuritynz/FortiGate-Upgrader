"""FortiGate Upgrader desktop entry point: runs the local API on 127.0.0.1 and opens a native window.

  python -m fortiupgrade            -> native window (pywebview)
  python -m fortiupgrade --browser  -> open in the default browser instead
"""
from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
import webbrowser

import uvicorn

from . import server


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve(port: int):
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")


def _wait_port(port: int, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser(prog="fortigate-upgrader")
    ap.add_argument("--browser", action="store_true", help="use the default web browser instead of a window")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--serve-only", action="store_true", help="run the local server without opening anything")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import updater
    updater.start_background_checks()
    port = args.port or _free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    _wait_port(port)
    url = f"http://127.0.0.1:{port}/#{server.TOKEN}"

    if args.serve_only:
        args.browser = True
    if not args.browser:
        try:
            import webview
        except ImportError:
            args.browser = True
    if args.browser:
        print(f"FortiGate Upgrader running at {url}  (Ctrl+C to quit)")
        if not args.serve_only:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    window = webview.create_window("FortiGate Upgrader", url, width=1400, height=900, min_size=(1000, 650))

    def picker():
        res = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                        file_types=("FortiOS images (*.out)", "All files (*.*)"))
        return list(res or [])

    server.FILE_PICKER = picker
    webview.start()


if __name__ == "__main__":
    main()
