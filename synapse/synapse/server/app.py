"""Synapse FastAPI server: REST + WebSocket, wires council, brain, RE tools,
resilience, cache, task registry, and discovery into one app.

Model addressing:
  - cloud model:  "moonshotai/kimi-k3"        (via primary ZenMux gateway)
  - local model:  "local::ollama::llama3.1"   (zero tokens)
  - extra gw:     "freebuff/minimax-m3"       (via Freebuff extra gateway)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from ..brain import Brain, learn_from_text
from ..cache import TTLCache
from ..config import AppConfig, load_config
from ..council import Orchestrator
from ..providers import OpenAICompatClient
from ..re import analyze as re_analyze
from ..re import compare as re_compare
from ..re import decompile as re_decompile
from ..re import memory as re_memory
from ..re import processes as re_processes
from ..resilience import call_with_recovery
from ..tasks import TaskRegistry

logger = logging.getLogger("synapse")

ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DIR = ROOT / "web"

# How long discovery results are cached (ports from HexStrike's cache layer)
DISCOVERY_TTL = 60.0


class SynapseState:
    """All shared server state in one place (easy to reset in tests)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.gateway = OpenAICompatClient(
            config.gateway.base_url, config.gateway.api_key, config.gateway.timeout, "gateway"
        )
        self.local_clients = {
            p.name: OpenAICompatClient(p.base_url, p.api_key, 120.0, p.name)
            for p in config.locals
        }
        # Peer-level extra gateways, addressed via "<name>/<model>" prefix.
        # Empty when no extra_gateways are configured.
        self.extra_gateway_clients = {
            g.name: (
                OpenAICompatClient(
                    g.base_url,
                    g.api_key,
                    g.timeout,
                    g.name,
                    g.auth_scheme,
                    g.auth_param_name,
                ),
                g,
            )
            for g in config.extra_gateways
        }
        self.brain = Brain(config.brain.path)
        self.cache = TTLCache(max_size=256, ttl=DISCOVERY_TTL)
        self.tasks = TaskRegistry()
        self.started = time.time()
        self.orchestrator = Orchestrator(self.resolve_model)
        self._discovery: dict | None = None

    # ------------------------------------------------------------- models
    def resolve_model(self, model_id: str):
        """Map a UI model id to (client, real_model_id).

        Routing precedence:
          local::<provider>::<real_id>     -> that local provider
          <extra_gateway>/<real_id>        -> that extra gateway
          <extra_gateway>  (bare, no /)    -> default_model of that gateway
          <id>                             -> primary gateway (ZenMux)
        """
        if model_id.startswith("local::"):
            _, provider, real_id = model_id.split("::", 2)
            client = self.local_clients.get(provider)
            if client is None:
                raise ValueError(f"unknown local provider: {provider}")
            return client, real_id

        # Extra gateways: "<name>/<model>" or bare "<name>".
        for g in self.config.extra_gateways:
            if model_id == g.name or model_id.startswith(f"{g.name}/"):
                real_id = "" if model_id == g.name else model_id[len(g.name) + 1:]
                if not real_id:
                    if not g.default_model:
                        raise ValueError(
                            f"gateway '{g.name}' requires explicit model: '{g.name}/<model>'"
                        )
                    real_id = g.default_model
                client, _ = self.extra_gateway_clients[g.name]
                return client, real_id

        # Default: primary gateway (ZenMux).
        return self.gateway, model_id

    def is_local(self, model_id: str) -> bool:
        return model_id.startswith("local::")

    async def discover(self, force: bool = False) -> dict:
        """Probe gateway + local servers + extra gateways for live model lists."""
        if not force and self._discovery and (time.time() - self._discovery["ts"]) < DISCOVERY_TTL:
            return self._discovery["data"]

        gateway_ids: list[str] = []
        gateway_ok = False
        if self.config.gateway.api_key:
            try:
                gateway_ids = await self.gateway.list_models()
                gateway_ok = True
            except Exception as e:
                logger.warning(f"gateway discovery failed: {e}")

        local: dict[str, dict] = {}
        for name, client in self.local_clients.items():
            entry = {"up": False, "models": []}
            try:
                entry["models"] = await client.list_models()
                entry["up"] = True
            except Exception:
                pass
            local[name] = entry

        extra_gateways: dict[str, dict] = {}
        for name, (client, _cfg) in self.extra_gateway_clients.items():
            entry = {"up": False, "models": []}
            # Attempt list_models() unconditionally; OpenAICompatClient._headers()
            # already skips emitting any auth header when api_key is empty, so no-
            # auth peers (e.g. a local OpenAI-compat shim) discover correctly
            # without a dummy env-var workaround.
            try:
                entry["models"] = await client.list_models()
                entry["up"] = True
            except Exception as e:
                logger.warning(f"extra gateway '{name}' discovery failed: {e}")
            extra_gateways[name] = entry

        data = {
            "gateway_ok": gateway_ok,
            "gateway_models": gateway_ids,
            "local": local,
            "extra_gateways": extra_gateways,
            "ts": time.time(),
        }
        self._discovery = {"data": data, "ts": time.time()}
        return data

    async def pick_free_model(self) -> tuple | None:
        """Pick a local model for background work (learning/decompile).
        Returns (client, "local::provider::model") or None."""
        disc = await self.discover()
        for name, entry in disc["local"].items():
            if entry["up"] and entry["models"]:
                mid = f"local::{name}::{entry['models'][0]}"
                client, _ = self.resolve_model(mid)
                return client, mid
        return None


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config()
    state = SynapseState(config)
    app = FastAPI(title="Synapse", version="0.1.0")
    app.state.synapse = state

    # ---------------------------------------------------------------- pages
    @app.get("/")
    async def index():
        page = WEB_DIR / "index.html"
        if not page.exists():
            return JSONResponse({"error": "web/index.html missing"}, 404)
        return FileResponse(page)

    @app.get("/health")
    async def health():
        return {"status": "ok", "uptime_s": round(time.time() - state.started, 1)}

    # ---------------------------------------------------------------- status
    @app.get("/api/status")
    async def status():
        disc = await state.discover()
        return {
            "gateway": {"ok": disc["gateway_ok"], "has_key": bool(config.gateway.api_key)},
            "local": {n: {"up": e["up"], "models": e["models"][:10]} for n, e in disc["local"].items()},
            "extra_gateways": {
                n: {
                    "up": e["up"],
                    "models": e["models"][:10],
                    "has_key": bool(state.extra_gateway_clients[n][1].api_key),
                }
                for n, e in disc["extra_gateways"].items()
            },
            "brain": state.brain.stats(),
            "tasks": state.tasks.stats(),
            "cache": state.cache.stats(),
        }

    # ---------------------------------------------------------------- models
    @app.get("/api/models")
    async def models():
        disc = await state.discover()
        roster = [
            {"id": m.id, "label": m.label, "provider": m.provider, "role": m.role, "local": False}
            for m in config.roster
        ]
        local_models = []
        for name, entry in disc["local"].items():
            for mid in entry["models"]:
                local_models.append(
                    {
                        "id": f"local::{name}::{mid}",
                        "label": f"{mid} ({name})",
                        "provider": name,
                        "role": "local",
                        "local": True,
                    }
                )
        return {
            "roster": roster + local_models,
            "discovered": {
                "gateway": disc["gateway_models"],
                "local": {n: e["models"] for n, e in disc["local"].items()},
                "extra_gateways": {n: e["models"] for n, e in disc["extra_gateways"].items()},
            },
        }

    @app.post("/api/models/discover")
    async def models_discover():
        disc = await state.discover(force=True)
        return {"ok": True, **disc}

    # ------------------------------------------------------------------- ws
    @app.websocket("/ws")
    async def ws_chat(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid JSON"})
                    continue

                if req.get("action") != "chat":
                    await ws.send_json({"type": "error", "message": "unknown action"})
                    continue

                mode = req.get("mode", "council")
                prompt = (req.get("prompt") or "").strip()
                models = [m for m in req.get("models", []) if m]
                chairman = req.get("chairman")
                pipeline = req.get("pipeline") or []
                use_brain = bool(req.get("use_brain", True))

                if not prompt or not models:
                    await ws.send_json({"type": "error", "message": "prompt and models required"})
                    continue

                extra = ""
                if use_brain:
                    extra = await asyncio.to_thread(state.brain.context_snippets, prompt, 5)

                result = {}
                try:
                    async for evt in state.orchestrator.run(
                        mode, prompt, models, chairman, pipeline, extra
                    ):
                        if evt["type"] == "done":
                            result = evt["result"]
                        await ws.send_json(evt)
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    await ws.send_json({"type": "error", "message": str(e)[:500]})

                # ---- second brain: learn from the session (zero-token first)
                if use_brain and result:
                    try:
                        material = prompt + "\n\n" + (result.get("synthesis") or result.get("final") or "")
                        added = await _learn(state, material, source=f"council:{mode}")
                        if added:
                            await ws.send_json({"type": "brain_update", "added": added})
                    except Exception:
                        pass
        except WebSocketDisconnect:
            return

    # ---------------------------------------------------------------- brain
    @app.get("/api/brain/stats")
    async def brain_stats():
        return state.brain.stats()

    @app.get("/api/brain/search")
    async def brain_search(q: str = "", limit: int = 20):
        return await asyncio.to_thread(state.brain.search, q, limit)

    @app.get("/api/brain/list")
    async def brain_list(kind: str | None = None, limit: int = 50):
        return await asyncio.to_thread(state.brain.list, kind, limit)

    @app.post("/api/brain/note")
    async def brain_note(body: dict):
        try:
            entry = state.brain.add(
                title=body.get("title", ""),
                content=body.get("content", ""),
                kind=body.get("kind", "note"),
                tags=body.get("tags", ""),
                source="manual",
            )
            return {"ok": True, **entry}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/brain/{entry_id}")
    async def brain_delete(entry_id: int):
        return {"ok": state.brain.delete(entry_id)}

    # ------------------------------------------------------------------- re
    @app.get("/api/re/processes")
    async def re_procs():
        cached = state.cache.get("processes")
        if cached is not None:
            return cached
        procs = await asyncio.to_thread(re_processes.list_processes)
        state.cache.set("processes", procs)
        return procs

    @app.get("/api/re/process/{pid}/detail")
    async def re_detail(pid: int):
        try:
            return await asyncio.to_thread(re_processes.process_detail, pid)
        except Exception as e:
            raise HTTPException(400, f"detail failed for pid {pid}: {e}")

    @app.post("/api/re/read")
    async def re_read(body: dict):
        pid, address, size = body.get("pid"), body.get("address"), int(body.get("size", 256))
        if not pid or address is None:
            raise HTTPException(400, "pid and address required")
        try:
            addr = _parse_addr(address)
            return await asyncio.to_thread(_do_read, pid, addr, min(size, 1024 * 1024))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except re_memory.MemoryError_ as e:
            raise HTTPException(400, str(e))

    @app.post("/api/re/write")
    async def re_write(body: dict):
        pid, address, hexstr = body.get("pid"), body.get("address"), body.get("hex", "")
        if not pid or address is None or not hexstr:
            raise HTTPException(400, "pid, address and hex required")
        try:
            addr = _parse_addr(address)
            data = bytes.fromhex(hexstr.replace(" ", ""))
        except ValueError as e:
            raise HTTPException(400, f"invalid address/hex: {e}")
        try:
            written = await asyncio.to_thread(_do_write, pid, addr, data)
            return {"written": written}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/re/scan")
    async def re_scan(body: dict):
        pid, pattern = body.get("pid"), body.get("pattern", "")
        limit = min(int(body.get("limit", 100)), 1000)
        if not pid or not pattern:
            raise HTTPException(400, "pid and pattern required")
        try:
            matches = await asyncio.to_thread(_do_scan, pid, pattern, limit)
            return {"matches": [f"0x{m:X}" for m in matches], "count": len(matches)}
        except re_memory.MemoryError_ as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------- analyze / compare
    @app.post("/api/re/analyze")
    async def re_analyze_ep(body: dict):
        source = body.get("source", "file")
        max_functions = min(int(body.get("max_functions", 60)), 300)
        use_llm = bool(body.get("use_llm", False))
        model = body.get("model")

        async def job(task_id: int):
            if source == "file":
                path = os.path.normpath(body.get("path", "").strip())
                if not path:
                    raise ValueError("path required for file source")
                analysis = await asyncio.to_thread(re_analyze.analyze_file, path, max_functions)
            else:
                pid = body.get("pid")
                if not pid:
                    raise ValueError("pid required for process source")
                mb = body.get("module_base")
                module_base = _parse_addr(mb) if mb else None
                analysis = await asyncio.to_thread(
                    re_analyze.analyze_process_module, int(pid), module_base, max_functions
                )

            if use_llm and model:
                state.tasks.progress(task_id, "decompiling with LLM")
                try:
                    client, real_id = state.resolve_model(model)
                    analysis["functions"] = await re_decompile.decompile_functions(
                        client, real_id, analysis["functions"]
                    )
                    analysis["decompiled_with"] = model
                except Exception as e:
                    analysis["decompile_error"] = str(e)[:300]

            # store in brain (kind=analysis) so it grows and is comparable later
            summary = (
                f"Target: {analysis['target']}\nSHA256: {analysis['sha256']}\n"
                f"Functions: {analysis['function_count']}\n"
                f"Imports: {analysis['pe']['import_count']} from {len(analysis['pe']['imports'])} DLLs\n"
                f"Top strings: {', '.join(analysis['strings'][:20])}"
            )
            entry = state.brain.add(
                title=f"Analysis: {analysis['target']}",
                content=summary,
                kind="analysis",
                tags="re,analysis",
                source="re_analyze",
            )
            analysis["brain_id"] = entry["id"]
            return analysis

        return await _run_task(state, "analyze", f"analyze {source}", job)

    @app.get("/api/re/analyses")
    async def re_analyses():
        """List past analyses stored in the brain."""
        entries = await asyncio.to_thread(state.brain.list, "analysis", 50)
        return [
            {"id": e["id"], "title": e["title"], "updated_at": e["updated_at"], "preview": e["content"][:300]}
            for e in entries
        ]

    @app.post("/api/re/compare")
    async def re_compare_ep(body: dict):
        ids = body.get("ids", [])
        use_llm = bool(body.get("use_llm", False))
        model = body.get("model")
        if len(ids) < 2:
            raise HTTPException(400, "select at least 2 analyses to compare")

        analyses = []
        for eid in ids:
            entry = state.brain.get(int(eid))
            if entry:
                analyses.append(_brain_entry_to_analysis(entry))
        if len(analyses) < 2:
            raise HTTPException(400, "could not load 2+ analyses from brain (analyzed yet?)")

        comparison = re_compare.compare_many(analyses)

        if use_llm and model:
            try:
                client, real_id = state.resolve_model(model)
                comparison["llm_summary"] = await re_compare.llm_compare_summary(
                    client, real_id, comparison
                )
            except Exception as e:
                comparison["llm_summary_error"] = str(e)[:300]

        # brain learns the comparison too
        await _learn(
            state,
            f"Compared {comparison['count']} targets: {', '.join(comparison['targets'])}. "
            f"avg string similarity {comparison['avg_string_similarity']}, identical={comparison['all_identical']}",
            source="re_compare",
        )
        return comparison

    # ---------------------------------------------------------------- tasks
    @app.get("/api/tasks")
    async def tasks_list():
        return state.tasks.list()

    @app.get("/api/tasks/{task_id}")
    async def tasks_get(task_id: int):
        t = state.tasks.get(task_id)
        if not t:
            raise HTTPException(404, "task not found")
        return t

    @app.post("/api/tasks/{task_id}/cancel")
    async def tasks_cancel(task_id: int):
        return {"ok": state.tasks.cancel(task_id)}

    return app


# ------------------------------------------------------------------ helpers
def _parse_addr(value) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def _do_read(pid: int, addr: int, size: int) -> dict:
    handle = re_memory.open_process(pid)
    try:
        data = re_memory.read_bytes(handle, addr, size)
        return {"hex": data.hex(" "), "dump": re_memory.hexdump(data, addr)}
    finally:
        re_memory.close_handle(handle)


def _do_write(pid: int, addr: int, data: bytes) -> int:
    handle = re_memory.open_process(pid, write=True)
    try:
        return re_memory.write_bytes(handle, addr, data)
    finally:
        re_memory.close_handle(handle)


def _do_scan(pid: int, pattern: str, limit: int) -> list[int]:
    handle = re_memory.open_process(pid)
    try:
        return re_memory.scan(handle, pattern, limit)
    finally:
        re_memory.close_handle(handle)


def _brain_entry_to_analysis(entry: dict) -> dict:
    """Rebuild a comparison-ready analysis dict from a stored brain entry."""
    import re as _re

    content = entry["content"]
    sha = (_re.search(r"SHA256:\s*([0-9a-f]+)", content) or [None, ""])[1]
    strings = []
    m = _re.search(r"Top strings:\s*(.+)", content)
    if m:
        strings = [s.strip() for s in m.group(1).split(",") if s.strip()]
    return {
        "target": entry["title"].replace("Analysis: ", ""),
        "sha256": sha,
        "strings": strings,
        "pe": {"imports": {}, "exports": []},
        "functions": [],
        "size_bytes": 0,
    }


async def _learn(state: SynapseState, material: str, source: str) -> list[dict]:
    """Learn into the brain, preferring a local (zero-token) model."""
    client = None
    model = None
    cfg = state.config.brain
    if cfg.prefer_local_for_learning:
        pick = await state.pick_free_model()
        if pick:
            client, _mid = pick
            _, model = state.resolve_model(_mid)
    if client is None and cfg.allow_cloud_learning:
        client = state.gateway
        model = state.config.roster[0].id if state.config.roster else None
    return await learn_from_text(state.brain, material, source, "lesson", client, model)


async def _run_task(state: SynapseState, kind: str, label: str, job) -> dict:
    """Run a job tracked by the task registry (ported task pattern)."""
    task_id = state.tasks.register(kind, label)
    try:
        result = await job(task_id)
        state.tasks.complete(task_id, {"ok": True})
        if isinstance(result, dict):
            result["task_id"] = task_id
        return result
    except asyncio.CancelledError:
        state.tasks.cancel(task_id)
        raise HTTPException(499, "task cancelled")
    except Exception as e:
        state.tasks.fail(task_id, str(e)[:500])
        raise HTTPException(400, f"{kind} failed: {e}")
