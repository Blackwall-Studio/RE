"""Tiny OpenAI-compatible local shim used for offline Synapse testing.

Serves the same /v1/{models,chat/completions} surface Synapse's
OpenAICompatClient expects, but without any auth. Used as a no-network
substitute for a cloud peer gateway (freebuff/cloud) when testing the
council router, the WebSocket flow, or any code path that needs a live
extra_gateway on the host.

Default binding: 127.0.0.1:8080 with models ["minimax-m3", "minimax-mini"]
(matches the conventional freebuff/<id> roster shape).

Override via env vars or CLI flags (CLI wins):

    set FREE_BUFF_LOCAL_PORT=1234
    set FREE_BUFF_LOCAL_MODELS=A,B,C
    set FREE_BUFF_LOCAL_HOST=127.0.0.1

    python scripts/freebuff_local_shim.py --port 1234 --models A,B,C

Run from the repo root: ``python scripts/freebuff_local_shim.py``. Stop with
Ctrl+C; if launched detached on Windows, ``taskkill /PID <pid> /F``.

Not for production — keep it simple. No auth, no logging, no streaming SSE.
Replace with LM Studio's built-in OpenAI-compatible server (also default
127.0.0.1:1234/v1) once you need real model output.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_MODELS = ["minimax-m3", "minimax-mini"]
DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tiny local OpenAI-compatible shim for Synapse testing."
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FREE_BUFF_LOCAL_PORT", DEFAULT_PORT)),
        help="TCP port to bind (default: 8080)",
    )
    p.add_argument(
        "--host",
        type=str,
        default=os.environ.get("FREE_BUFF_LOCAL_HOST", DEFAULT_HOST),
        help="Interface to bind (default: 127.0.0.1)",
    )
    p.add_argument(
        "--models",
        type=str,
        default=os.environ.get(
            "FREE_BUFF_LOCAL_MODELS", ",".join(DEFAULT_MODELS)
        ),
        help="Comma-separated model ids advertised on /v1/models "
        "(default: minimax-m3,minimax-mini)",
    )
    return p.parse_args()


class ShimHandler(BaseHTTPRequestHandler):
    """Routes /v1/models + /v1/chat/completions to static JSON responses.

    Invariant: ``models`` is mutated once from ``main()`` BEFORE
    ``HTTPServer.serve_forever()``. HTTPServer's default single-threaded loop
    makes this safe; touching ``models`` from inside a request handler body
    would race under ThreadingHTTPServer.
    """

    # Set from main() before serve_forever().
    models: list[str] = list(DEFAULT_MODELS)

    def _send(self, code: int, body: dict) -> None:
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler casing)
        if self.path == "/v1/models":
            return self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": m, "object": "model", "owned_by": "shim"}
                        for m in self.models
                    ],
                },
            )
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                req = {}
            model = req.get("model") or (
                self.models[0] if self.models else "shim"
            )
            return self._send(
                200,
                {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:8],
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": f"[shim reply for {model}]",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                },
            )
        return self._send(404, {"error": "not found"})

    def log_message(self, *args, **kwargs) -> None:  # noqa: D401
        # Suppress access logs — this shim is a fixture, not a service to monitor.
        pass


def main() -> None:
    args = parse_args()
    ShimHandler.models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not ShimHandler.models:
        raise SystemExit(
            "[shim] --models produced empty list; pass at least one id "
            "(e.g. --models minimax-m3,minimax-mini)"
        )
    # Refuse wide-area binding unless explicitly opted-in via env var. Default
    # 127.0.0.1 binds loopback only; a public listener is undesirable for a
    # test fixture.
    if args.host in ("0.0.0.0", "::") and "FREE_BUFF_LOCAL_ALLOW_ANY" not in os.environ:
        raise SystemExit(
            f"[shim] refusing to bind {args.host} (public listener); set "
            f"FREE_BUFF_LOCAL_ALLOW_ANY=1 in env to override."
        )
    print(
        f"[shim] listening on http://{args.host}:{args.port}  "
        f"models={ShimHandler.models}",
        flush=True,
    )
    HTTPServer((args.host, args.port), ShimHandler).serve_forever()


if __name__ == "__main__":
    main()
