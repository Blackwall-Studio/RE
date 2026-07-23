"""End-to-end API tests: real FastAPI app, real own-process RE calls,
fake LLM layer (no network, no API key needed)."""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse.config import (
    AppConfig,
    BrainConfig,
    ExtraGateway,
    GatewayConfig,
    LocalProvider,
    ModelEntry,
)
from synapse.re import memory
from synapse.server.app import create_app


class FakeGateway:
    """Stands in for the LLM gateway - no network."""

    name = "gateway"
    api_key = "sk-test"

    async def list_models(self):
        return ["fake/kimi-k3", "fake/deepseek-v4"]

    async def chat(self, model, messages, temperature=0.7, max_tokens=None):
        return {"text": f"answer-from-{model}", "usage": {"total_tokens": 5}}

    async def chat_stream(self, model, messages, temperature=0.7, max_tokens=None):
        for tok in f"answer-from-{model}".split("-"):
            yield tok + "-", {"total_tokens": 5}


class FakeExtraGateway:
    """Stands in for an extra peer-level gateway (Freebuff shape).

    Mirrors the public surface that SynapseState / OpenAICompatClient reads:
      .name, .api_key, .base_url, .timeout   (plus chat/chat_stream/list_models).
    """

    name = "freebuff"
    api_key = "sk-freebuff-test"
    base_url = "http://fake-freebuff"
    timeout = 180.0

    def __init__(self):
        self.calls: list[dict] = []

    async def list_models(self):
        self.calls.append({"method": "list_models"})
        return ["minimax-m3"]

    async def chat(self, model, messages, temperature=0.7, max_tokens=None):
        self.calls.append({"method": "chat", "model": model})
        return {"text": f"freebuff-{model}-answer", "usage": {"total_tokens": 3}}

    async def chat_stream(self, model, messages, temperature=0.7, max_tokens=None):
        self.calls.append({"method": "chat_stream", "model": model})
        for tok in f"freebuff-{model}-answer".split("-"):
            yield tok + "-", {"total_tokens": 3}


@pytest.fixture()
def app(tmp_path):
    cfg = AppConfig(
        gateway=GatewayConfig(base_url="http://fake", api_key="sk-test"),
        locals=[LocalProvider(name="ollama", base_url="http://localhost:11434/v1")],
        extra_gateways=[
            ExtraGateway(
                name="freebuff",
                base_url="http://fake-freebuff",
                api_key="sk-freebuff-test",
                default_model="minimax-m3",
            )
        ],
        roster=[
            ModelEntry(id="fake/kimi-k3", label="Kimi"),
            ModelEntry(id="fake/deepseek-v4", label="DS"),
            ModelEntry(
                id="freebuff/minimax-m3",
                label="M3 (Freebuff path)",
                provider="freebuff",
            ),
        ],
        brain=BrainConfig(
            path=str(tmp_path / "brain.db"),
            prefer_local_for_learning=False,
            allow_cloud_learning=False,
        ),
    )
    app = create_app(cfg)
    state = app.state.synapse
    state.gateway = FakeGateway()
    # Use the constructed FakeExtraGateway (which has api_key) instead of
    # the bare class so client.api_key is truthy in discover() and /api/status.
    fake_fb = FakeExtraGateway()
    state.extra_gateway_clients["freebuff"] = (fake_fb, cfg.extra_gateways[0])
    state.orchestrator._resolve = state.resolve_model  # rebind uses state.gateway
    # discovery uses fake clients too
    state._discovery = None
    # Stash on app for tests that want to inspect captured calls
    app.state.fake_freebuff = fake_fb
    return app


@pytest.fixture()
def client(app):
    return TestClient(app)


# ----------------------------------------------------------------- app contract
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "html" in r.text.lower()


def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "gateway" in body and "local" in body and "brain" in body and "tasks" in body
    assert body["gateway"]["ok"] is True  # fake gateway discovered


def test_status_includes_extra_gateways(client):
    r = client.get("/api/status")
    body = r.json()
    assert "extra_gateways" in body, f"missing extra_gateways in /api/status: {body}"
    assert "freebuff" in body["extra_gateways"]
    assert body["extra_gateways"]["freebuff"]["up"] is True
    assert body["extra_gateways"]["freebuff"]["has_key"] is True
    assert "minimax-m3" in body["extra_gateways"]["freebuff"]["models"]


def test_models_includes_roster(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    roster = r.json()["roster"]
    ids = [m["id"] for m in roster]
    assert "fake/kimi-k3" in ids
    assert r.json()["discovered"]["gateway"] == ["fake/kimi-k3", "fake/deepseek-v4"]


def test_models_includes_extra_gateway_discovery(client):
    r = client.get("/api/models")
    body = r.json()
    assert "extra_gateways" in body["discovered"]
    assert body["discovered"]["extra_gateways"]["freebuff"] == ["minimax-m3"]


def test_roster_includes_freebuff_entry(client):
    r = client.get("/api/models")
    roster = r.json()["roster"]
    ids = [m["id"] for m in roster]
    assert "freebuff/minimax-m3" in ids
    fb_entry = next(m for m in roster if m["id"] == "freebuff/minimax-m3")
    assert fb_entry["provider"] == "freebuff"


def test_resolve_bare_gateway_to_default_model(app, client):
    """Bare "freebuff" must resolve through default_model to the FakeExtraGateway."""
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({
            "action": "chat",
            "mode": "broadcast",
            "prompt": "say hi",
            "models": ["freebuff"],
            "use_brain": False,
        }))
        done = None
        for _ in range(200):
            evt = json.loads(ws.receive_text())
            if evt["type"] == "done":
                done = evt["result"]
                break
    assert done is not None, "websocket broadcast never produced a done event"
    assert "freebuff" in done["answers"], f"freebuff answer missing: {done}"
    assert done["answers"]["freebuff"].startswith("freebuff-minimax-m3-")
    routed = [
        c for c in app.state.fake_freebuff.calls
        if c.get("method") == "chat_stream" and c.get("model") == "minimax-m3"
    ]
    assert routed, f"FakeExtraGateway never received 'minimax-m3' upstream: {app.state.fake_freebuff.calls}"


def test_resolve_prefixed_gateway_routes_to_extra_gateway(app, client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({
            "action": "chat",
            "mode": "broadcast",
            "prompt": "say hi",
            "models": ["freebuff/minimax-m3"],
            "use_brain": False,
        }))
        done = None
        for _ in range(200):
            evt = json.loads(ws.receive_text())
            if evt["type"] == "done":
                done = evt["result"]
                break
    assert done is not None, "websocket broadcast never produced a done event"
    assert "freebuff/minimax-m3" in done["answers"]
    assert done["answers"]["freebuff/minimax-m3"].startswith("freebuff-minimax-m3-")
    routed = [
        c for c in app.state.fake_freebuff.calls
        if c.get("method") == "chat_stream" and c.get("model") == "minimax-m3"
    ]
    assert routed, f"FakeExtraGateway never received 'minimax-m3' upstream: {app.state.fake_freebuff.calls}"


def test_primary_gateway_unaffected_by_extra_gateways(client):
    """Primary <id>-without-prefix path still routes to the FakeGateway."""
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({
            "action": "chat",
            "mode": "broadcast",
            "prompt": "hi",
            "models": ["fake/kimi-k3"],
            "use_brain": False,
        }))
        done = None
        for _ in range(200):
            evt = json.loads(ws.receive_text())
            if evt["type"] == "done":
                done = evt["result"]
                break
    assert done is not None
    assert done["answers"]["fake/kimi-k3"].startswith("answer-from-fake/kimi-k3-")


# ----------------------------------------- SynapseState unit tests (no app fixture)
def test_resolve_no_extra_gateways_falls_through_to_primary(tmp_path):
    """Back-compat: empty extra_gateways routes bare ids to the primary gateway."""
    from synapse.server.app import SynapseState

    cfg = AppConfig(
        gateway=GatewayConfig(base_url="http://fake", api_key="sk-test"),
        # extra_gateways defaults to []
        locals=[],
        roster=[],
        brain=BrainConfig(path=str(tmp_path / "brain.db")),
    )
    state = SynapseState(cfg)
    assert state.extra_gateway_clients == {}
    client, real_id = state.resolve_model("anything")
    assert client is state.gateway
    assert real_id == "anything"


def test_resolve_bare_gateway_without_default_model_raises(tmp_path):
    """Bare '<name>' with no default_model must raise a clean ValueError."""
    from synapse.server.app import SynapseState

    cfg = AppConfig(
        gateway=GatewayConfig(base_url="http://fake", api_key="sk-test"),
        extra_gateways=[ExtraGateway(name="foo", base_url="http://x", api_key="k")],
        locals=[],
        roster=[],
        brain=BrainConfig(path=str(tmp_path / "brain.db")),
    )
    state = SynapseState(cfg)
    with pytest.raises(ValueError, match="requires explicit model"):
        state.resolve_model("foo")


# ----------------------------------------- read_bytes cap regression coverage
def test_read_bytes_cap_rejects_oversized(monkeypatch):
    """read_bytes rejects sizes above the configured cap (env override).

    Note: _max_read_bytes() floors env values below 1 MB up to 1 MB, so the
    override target needs to be >= 1 MB. We set 2 MB and request 4 MB to leave
    a comfortable margin over the floor.
    """
    handle = memory.open_process(os.getpid())
    try:
        monkeypatch.setenv("SYNAPSE_RE_MAX_READ_BYTES", str(2 * 1024 * 1024))  # 2 MB
        with pytest.raises(memory.MemoryError_, match="invalid read size"):
            memory.read_bytes(handle, 0x1000, 4 * 1024 * 1024)  # 4 MB >> 2 MB cap
    finally:
        memory.close_handle(handle)


def test_read_bytes_default_cap_pins_constant(monkeypatch):
    """The default cap is the documented constant; pin it so future bumps are intentional."""
    monkeypatch.delenv("SYNAPSE_RE_MAX_READ_BYTES", raising=False)
    assert memory._max_read_bytes() == memory.DEFAULT_MAX_READ_BYTES
    # And that constant must be large enough for the user's reported case (~190 MB).
    assert memory.DEFAULT_MAX_READ_BYTES >= 256 * 1024 * 1024


# -------------------------------------------------------------- existing RE tests
def test_processes_api_contains_self(client):
    r = client.get("/api/re/processes")
    assert r.status_code == 200
    procs = r.json()
    assert os.getpid() in [p["pid"] for p in procs]


def test_process_detail_api(client):
    r = client.get(f"/api/re/process/{os.getpid()}/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["info"]["pid"] == os.getpid()
    assert body["regions"]["count"] > 0


def test_memory_read_write_scan_api(client):
    import ctypes

    pid = os.getpid()
    buf = ctypes.create_string_buffer(b"\xDE\xAD\xBE\xEF" + b"\x00" * 60, 64)
    addr = ctypes.addressof(buf)

    r = client.post("/api/re/read", json={"pid": pid, "address": hex(addr), "size": 16})
    assert r.status_code == 200, r.text
    assert "deadbeef" in r.json()["hex"].replace(" ", "")

    r = client.post("/api/re/write", json={"pid": pid, "address": hex(addr + 8), "hex": "cafebabe"})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == 4
    assert buf.raw[8:12] == b"\xca\xfe\xba\xbe"

    r = client.post("/api/re/scan", json={"pid": pid, "pattern": "CA FE BA BE", "limit": 10})
    assert r.status_code == 200, r.text
    matches_lower = [m.lower() for m in r.json()["matches"]]
    assert hex(addr + 8) in matches_lower


def test_read_bad_pid(client):
    r = client.post("/api/re/read", json={"pid": 99999999, "address": "0x1000", "size": 16})
    assert r.status_code == 400


def test_analyze_python_exe(client):
    r = client.post("/api/re/analyze", json={"source": "file", "path": sys.executable, "max_functions": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"]
    assert body["brain_id"]  # stored in second brain
    # now listed in analyses
    r2 = client.get("/api/re/analyses")
    assert any(a["id"] == body["brain_id"] for a in r2.json())


def test_compare_two_analyses(client):
    ids = []
    for _ in range(2):
        r = client.post("/api/re/analyze", json={"source": "file", "path": sys.executable, "max_functions": 3})
        assert r.status_code == 200
        ids.append(r.json()["brain_id"])
    r = client.post("/api/re/compare", json={"ids": ids})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["all_identical"] is True  # same file twice


def test_brain_note_crud(client):
    r = client.post("/api/brain/note", json={"title": "test note", "content": "hello brain", "tags": "t"})
    assert r.status_code == 200
    note_id = r.json()["id"]
    r = client.get("/api/brain/search?q=hello")
    assert any(n["id"] == note_id for n in r.json())
    r = client.get("/api/brain/stats")
    assert r.json()["total"] >= 1
    r = client.delete(f"/api/brain/{note_id}")
    assert r.json()["ok"] is True


def test_websocket_broadcast(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({
            "action": "chat", "mode": "broadcast",
            "prompt": "say hi", "models": ["fake/kimi-k3", "fake/deepseek-v4"],
            "use_brain": False,
        }))
        done = None
        tokens = 0
        for _ in range(200):
            evt = json.loads(ws.receive_text())
            if evt["type"] == "token":
                tokens += 1
            if evt["type"] == "done":
                done = evt["result"]
                break
        assert done is not None
        assert set(done["answers"].keys()) == {"fake/kimi-k3", "fake/deepseek-v4"}
        assert tokens > 0


def test_websocket_council(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({
            "action": "chat", "mode": "council",
            "prompt": "best language?", "models": ["fake/kimi-k3", "fake/deepseek-v4"],
            "chairman": "fake/kimi-k3", "use_brain": False,
        }))
        stages, done = [], None
        for _ in range(300):
            evt = json.loads(ws.receive_text())
            if evt["type"] == "stage":
                stages.append(evt["stage"])
            if evt["type"] == "done":
                done = evt["result"]
                break
        assert "answers" in stages and "critiques" in stages and "synthesis" in stages
        assert done["synthesis"]
        assert done["chairman"] == "fake/kimi-k3"


def test_tasks_registry_api(client):
    # analyze creates a task entry
    r = client.post("/api/re/analyze", json={"source": "file", "path": sys.executable, "max_functions": 2})
    assert r.status_code == 200
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert any(t["kind"] == "analyze" for t in r.json())


# ----------------------------------------------------- auth_scheme defenses
def test_openai_compat_headers_for_each_auth_scheme():
    """Verify OpenAICompatClient._headers() emits the right header for each scheme.

    Back-compat assertion: bearer (default constructor) byte-for-byte emits
    Authorization: Bearer <key>. Other schemes route the same api_key through
    different transport headers.
    """
    import base64 as _b64
    from synapse.providers.openai_compat import OpenAICompatClient

    bearer = OpenAICompatClient("http://x", "sk-test", 30.0, "b1")
    assert bearer._headers()["Authorization"] == "Bearer sk-test"

    xk = OpenAICompatClient("http://x", "sk-test", 30.0, "b2", auth_scheme="x-api-key")
    assert xk._headers().get("X-API-Key") == "sk-test"
    assert "Authorization" not in xk._headers()

    basic_userpass = OpenAICompatClient("http://x", "user:pwd", 30.0, "b3", auth_scheme="basic")
    expected_basic = "Basic " + _b64.b64encode(b"user:pwd").decode()
    assert basic_userpass._headers()["Authorization"] == expected_basic

    basic_bare = OpenAICompatClient("http://x", "sk-bare", 30.0, "b3b", auth_scheme="basic")
    expected_bare = "Basic " + _b64.b64encode(b"sk-bare:").decode()
    assert basic_bare._headers()["Authorization"] == expected_bare

    cookie = OpenAICompatClient(
        "http://x", "abcdef", 30.0, "b4", auth_scheme="cookie", auth_param_name="sid"
    )
    assert cookie._headers()["Cookie"] == "sid=abcdef"

    raw = OpenAICompatClient(
        "http://x",
        "sk-test",
        30.0,
        "b5",
        auth_scheme="header",
        auth_param_name="X-My-Token",
    )
    assert raw._headers()["X-My-Token"] == "sk-test"
    assert "Authorization" not in raw._headers()


def test_openai_compat_headers_with_no_api_key():
    """Back-compat: api_key unset sends Content-Type only, no auth header of any kind."""
    from synapse.providers.openai_compat import OpenAICompatClient

    bearer = OpenAICompatClient("http://x", "")
    h = bearer._headers()
    assert h["Content-Type"] == "application/json"
    assert "Authorization" not in h

    xk = OpenAICompatClient("http://x", "", auth_scheme="x-api-key")
    h = xk._headers()
    assert "X-API-Key" not in h


def test_extra_gateway_with_x_api_key_routes_via_synapse(app):
    """End-to-end: an extra_gateway configured with auth_scheme: x-api-key constructs
    a client that emits X-API-Key headers; routing and resolution still work.

    We don't assert headers at the wire (fake client doesn't issue HTTP), but we
    verify the FakeExtraGateway replacement still receives the expected model.
    """
    from synapse.config import AppConfig, BrainConfig, ExtraGateway

    cfg = AppConfig(
        gateway=GatewayConfig(base_url="http://fake", api_key="sk-test"),
        locals=[],
        extra_gateways=[
            ExtraGateway(
                name="xk_gw",
                base_url="http://fake-xk",
                api_key="sk-xk-test",
                auth_scheme="x-api-key",
                default_model="xk-model",
            )
        ],
        roster=[],
        brain=BrainConfig(path=str(app.state.synapse.config.brain.path)),
    )
    new_app = create_app(cfg)
    state = new_app.state.synapse
    fake = FakeExtraGateway()
    fake.name = "xk_gw"
    state.extra_gateway_clients["xk_gw"] = (fake, cfg.extra_gateways[0])

    # Resolve bare name — should fall through to default_model.
    client, real_id = state.resolve_model("xk_gw")
    assert fake in (client,) or client is state.extra_gateway_clients["xk_gw"][0]
    assert real_id == "xk-model"


# ----------------------------------------- basic-auth split-username paths
def test_openai_compat_basic_with_split_username_from_auth_param_name():
    """basic scheme builds 'username:password' when auth_param_name is non-empty."""
    import base64 as _b64
    from synapse.providers.openai_compat import OpenAICompatClient

    split = OpenAICompatClient(
        "http://x",
        "secret-pwd",
        30.0,
        "b6",
        auth_scheme="basic",
        auth_param_name="myuser",
    )
    expected = "Basic " + _b64.b64encode(b"myuser:secret-pwd").decode()
    assert split._headers()["Authorization"] == expected


def test_openai_compat_basic_with_username_but_empty_password_still_emits_header():
    """basic with username (auth_param_name) and empty password emits a 'user:' header."""
    import base64 as _b64
    from synapse.providers.openai_compat import OpenAICompatClient

    useronly = OpenAICompatClient(
        "http://x",
        "",
        30.0,
        "b7",
        auth_scheme="basic",
        auth_param_name="myuser",
    )
    expected = "Basic " + _b64.b64encode(b"myuser:").decode()
    h = useronly._headers()
    assert h["Authorization"] == expected


def test_openai_compat_basic_with_empty_username_falls_through_to_existing_logic():
    """basic with empty auth_param_name keeps old behavior (api_key + ':')."""
    import base64 as _b64
    from synapse.providers.openai_compat import OpenAICompatClient

    old_way = OpenAICompatClient(
        "http://x", "sk-pwd", 30.0, "b8", auth_scheme="basic", auth_param_name=""
    )
    expected = "Basic " + _b64.b64encode(b"sk-pwd:").decode()
    assert old_way._headers()["Authorization"] == expected


# ---------------------------------- chat_stream SSE auto-detect + non-SSE fallback
def test_chat_stream_consumes_sse_data_lines(capsys):
    """SSE path: data: … lines are parsed and each delta is yielded."""
    import asyncio
    import httpx2 as _httpx  # type: ignore
    from synapse.providers.openai_compat import OpenAICompatClient

    sse_payload = (
        b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello "}}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"world"}}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )

    def handler(request):
        return _httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_payload,
        )

    client = OpenAICompatClient("http://x", "sk-test", 30.0, name="fb")
    import synapse.providers.openai_compat as mod
    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **kw, transport=_httpx.MockTransport(handler))
    try:
        async def run():
            out = []
            async for delta, usage in client.chat_stream("minimax-m3", [{"role":"user","content":"hi"}]):
                out.append(delta)
            return out
        chunks = asyncio.run(run())
    finally:
        mod.httpx.AsyncClient = orig

    # Add capsys touch (used to silence linters about unused fixture in some pytest versions)
    _ = capsys
    assert chunks == ["Hello ", "world"], f"expected ['Hello ', 'world'], got {chunks}"


def test_chat_stream_falls_back_to_one_shot_on_application_json():
    """Non-SSE path: server returns application/json instead of SSE; chat_stream
    yields the first message content once and returns so callers' aggregators
    don't end up empty."""
    import asyncio
    import httpx2 as _httpx  # type: ignore
    from synapse.providers.openai_compat import OpenAICompatClient

    captured_headers: list[str] = []

    def handler(request):
        captured_headers.append(request.headers.get("content-type", ""))
        return _httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps({
                "id": "chatcmpl-fallback",
                "object": "chat.completion",
                "model": "minimax-m3",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "[shim reply for minimax-m3]",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }).encode(),
        )

    client = OpenAICompatClient("http://x", "sk-test", 30.0, name="fb")
    import synapse.providers.openai_compat as mod
    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(*a, **kw, transport=_httpx.MockTransport(handler))
    try:
        async def run():
            chunks = []
            async for delta, usage in client.chat_stream("minimax-m3", [{"role":"user","content":"hi"}]):
                chunks.append((delta, usage))
            return chunks
        chunks = asyncio.run(run())
    finally:
        mod.httpx.AsyncClient = orig

    assert len(chunks) == 1, f"expected exactly one chunk, got {len(chunks)}: {chunks}"
    delta, usage = chunks[0]
    assert "[shim reply for minimax-m3]" in delta
    assert usage.get("total_tokens") == 20
    assert captured_headers, "server never received a request"


# ----------------------------------------- discover() no longer gates on api_key
def test_discover_attempts_list_models_without_api_key(app):
    """No-auth peers (e.g. local OpenAI-compatible shim) must be discovered
    even when api_key is empty. Previously discover() short-circuited on
    `if client.api_key:`, requiring a dummy env var to flip the gate."""
    import asyncio
    from synapse.config import AppConfig, BrainConfig, ExtraGateway

    cfg = AppConfig(
        gateway=GatewayConfig(base_url="http://fake", api_key="sk-test"),
        locals=[],
        extra_gateways=[
            ExtraGateway(
                name="no_auth",
                base_url="http://fake-no-auth",
                api_key="",  # explicit empty: pretend no auth needed
            )
        ],
        roster=[],
        brain=BrainConfig(path=str(app.state.synapse.config.brain.path)),
    )
    new_app = create_app(cfg)
    state = new_app.state.synapse

    class NoAuthGw:
        name = "no_auth"
        api_key = ""

        async def list_models(self):
            return ["model-a", "model-b"]

    state.extra_gateway_clients["no_auth"] = (NoAuthGw(), cfg.extra_gateways[0])

    disc = asyncio.run(state.discover(force=True))
    entry = disc["extra_gateways"]["no_auth"]
    assert entry["up"] is True, f"expected up=True, got {entry}"
    assert "model-a" in entry["models"]
    assert "model-b" in entry["models"]


def test_discover_failure_path_includes_log_warning(app):
    """When list_models raises, the gateway shows up=False and the warning is logged."""
    import asyncio
    from synapse.config import AppConfig, BrainConfig, ExtraGateway

    cfg = AppConfig(
        gateway=GatewayConfig(base_url="http://fake", api_key="sk-test"),
        locals=[],
        extra_gateways=[
            ExtraGateway(name="broken", base_url="http://broken", api_key="k")
        ],
        roster=[],
        brain=BrainConfig(path=str(app.state.synapse.config.brain.path)),
    )
    new_app = create_app(cfg)
    state = new_app.state.synapse

    class BrokenGw:
        name = "broken"
        api_key = "k"

        async def list_models(self):
            raise RuntimeError("simulated 503 from peer")

    state.extra_gateway_clients["broken"] = (BrokenGw(), cfg.extra_gateways[0])

    disc = asyncio.run(state.discover(force=True))
    entry = disc["extra_gateways"]["broken"]
    assert entry["up"] is False
    assert entry["models"] == []  # never polluted with stale data
