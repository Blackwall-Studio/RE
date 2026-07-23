"""OpenAI-compatible async client.

Works for the ZenMux gateway AND local servers (Ollama /v1, LM Studio /v1),
so cloud and local models share one code path. Local providers simply pass a
dummy key and pay zero tokens.
"""
from __future__ import annotations

import base64
import json
from typing import AsyncIterator

import httpx2 as httpx


class ChatError(Exception):
    """Raised when a chat completion request fails."""


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 180.0,
        name: str = "gateway",
        auth_scheme: str = "bearer",
        auth_param_name: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.name = name
        # auth_scheme + auth_param_name drive _headers() per-call.
        # Supported: bearer (default back-compat), x-api-key, basic, cookie, header.
        # Unknown scheme values fall through to bearer (matches previous behavior).
        self.auth_scheme = (auth_scheme or "bearer").lower()
        self.auth_param_name = auth_param_name or ""

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        scheme = self.auth_scheme
        # Empty api_key short-circuit, EXCEPT for basic auth where a username-only
        # header (base64('username:')  with empty password) is a valid Basic-Auth
        # sentinel when the server treats api_key as password and auth_param_name
        # is the separately-sourced username.
        if not self.api_key and not (scheme == "basic" and self.auth_param_name):
            return h
        if scheme == "x-api-key":
            h["X-API-Key"] = self.api_key
        elif scheme == "basic":
            if self.auth_param_name:
                ak = f"{self.auth_param_name}:{self.api_key}"
            else:
                ak = self.api_key if ":" in self.api_key else f"{self.api_key}:"
            h["Authorization"] = "Basic " + base64.b64encode(ak.encode()).decode()
        elif scheme == "cookie":
            name = self.auth_param_name or "Authorization"
            h["Cookie"] = f"{name}={self.api_key}"
        elif scheme == "header":
            name = self.auth_param_name or "X-Custom-Auth"
            h[name] = self.api_key
        else:  # bearer (and any unknown scheme) — back-compat default
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
            data = r.json()
            return sorted(m["id"] for m in data.get("data", []) if m.get("id"))

    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> dict:
        """Non-streaming completion. Returns {"text": str, "usage": dict}."""
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(
                f"{self.base_url}/chat/completions", headers=self._headers(), json=payload
            )
            if r.status_code != 200:
                raise ChatError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}")
            d = r.json()
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            return {"text": msg.get("content") or "", "usage": d.get("usage") or {}}

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Streaming completion. Yields (delta_text, usage_so_far).

        Falls back to a one-shot yield when the server ignores our
        ``stream: true`` flag and returns a regular JSON body instead of
        ``text/event-stream``. Detection is via ``Content-Type``: any non-SSE
        response yields the first choice's message content once and returns.
        Lets council-mode round trips still aggregate answers against peers
        that don't speak SSE even though we asked for streaming.
        """
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        usage: dict = {}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            async with c.stream(
                "POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=payload
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise ChatError(f"{self.name} HTTP {r.status_code}: {body[:300]}")
                ct = (r.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in ct:
                    # Peer returned a regular JSON body — yielded as a single chunk.
                    raw = await r.aread()
                    try:
                        d = json.loads(raw or b"{}")
                    except Exception:
                        d = {}
                    # Some misbehaving OpenAI-compat gateways return HTTP 200 + an
                    # error JSON body instead of using 4xx. Surface as ChatError
                    # so callers see the failure consistently with SSE 4xx path.
                    if "error" in d and "choices" not in d:
                        raise ChatError(
                            f"{self.name} returned error JSON: {str(d.get('error'))[:300]}"
                        )
                    msg = (d.get("choices") or [{}])[0].get("message") or {}
                    yield (msg.get("content") or ""), (d.get("usage") or {})
                    return
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("usage"):
                        usage = evt["usage"]
                    for ch in evt.get("choices", []):
                        delta = (ch.get("delta") or {}).get("content")
                        if delta:
                            yield delta, usage
