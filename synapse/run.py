#!/usr/bin/env python3
"""Synapse launcher: starts the server and opens the UI.

Usage:
  python run.py              # serve on 127.0.0.1:8000 and open browser
  python run.py --port 9000
  python run.py --no-browser
"""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="Synapse server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    import uvicorn

    url = f"http://{args.host}:{args.port}"
    print(f"[synapse] starting on {url}")
    print("[synapse] council + RE workbench + second brain ready")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "synapse.server.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level="info",
        ws="websockets",
    )


if __name__ == "__main__":
    sys.exit(main())
