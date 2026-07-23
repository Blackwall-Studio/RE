#!/usr/bin/env python3
"""Synapse MCP Server — exposes Synapse capabilities to MCP-compatible AI
agents (Claude Desktop, Cursor, VS Code Copilot, opencode).

Architecture mirrors HexStrike's two-process design: this MCP server talks to
the running Synapse HTTP server (default http://127.0.0.1:8000).

Run:  python synapse_mcp.py --server http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="[synapse-mcp] %(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

DEFAULT_SERVER = "http://127.0.0.1:8000"
TIMEOUT = 300

_server = DEFAULT_SERVER


def _post(endpoint: str, payload: dict) -> dict:
    url = f"{_server.rstrip('/')}/{endpoint.lstrip('/')}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:400]}"}
    except Exception as e:
        return {"error": f"request failed: {e}"}


def _get(endpoint: str) -> object:
    url = f"{_server.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": f"request failed: {e}"}


def build_mcp():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("synapse")

    @mcp.tool()
    def status() -> dict:
        """Synapse server status: gateway/local LLMs, brain size, tasks."""
        return _get("api/status")

    @mcp.tool()
    def list_models() -> dict:
        """List all available models (cloud roster + local zero-token models)."""
        return _get("api/models")

    @mcp.tool()
    def list_processes() -> list:
        """List running processes with memory usage and connection counts."""
        return _get("api/re/processes")

    @mcp.tool()
    def process_detail(pid: int) -> dict:
        """Deep inspection of a process: modules, threads, network connections, memory map."""
        return _get(f"api/re/process/{pid}/detail")

    @mcp.tool()
    def read_memory(pid: int, address: str, size: int = 256) -> dict:
        """Read memory from a live process. address like '0x7FF000'."""
        return _post("api/re/read", {"pid": pid, "address": address, "size": size})

    @mcp.tool()
    def scan_memory(pid: int, pattern: str, limit: int = 100) -> dict:
        """Pattern-scan process memory, e.g. '48 8B ?? 00'. Returns matching addresses."""
        return _post("api/re/scan", {"pid": pid, "pattern": pattern, "limit": limit})

    @mcp.tool()
    def analyze_binary(path: str, max_functions: int = 60, use_llm: bool = False, model: str = "") -> dict:
        """Static RE of a PE file: headers, imports, strings, disassembly, optional LLM decompile."""
        return _post(
            "api/re/analyze",
            {"source": "file", "path": path, "max_functions": max_functions, "use_llm": use_llm, "model": model},
        )

    @mcp.tool()
    def analyze_process(pid: int, max_functions: int = 60, use_llm: bool = False, model: str = "") -> dict:
        """Dump the main module of a LIVE process and fully analyze it."""
        return _post(
            "api/re/analyze",
            {"source": "process", "pid": pid, "max_functions": max_functions, "use_llm": use_llm, "model": model},
        )

    @mcp.tool()
    def compare_analyses(ids: list[int], use_llm: bool = False, model: str = "") -> dict:
        """Broad comparison of 2+ stored analyses (similarity, diffs, LLM summary)."""
        return _post("api/re/compare", {"ids": ids, "use_llm": use_llm, "model": model})

    @mcp.tool()
    def brain_search(query: str, limit: int = 10) -> list:
        """Search the second brain (persistent learned knowledge)."""
        return _get(f"api/brain/search?q={urllib.parse.quote(query)}&limit={limit}")

    @mcp.tool()
    def brain_add(title: str, content: str, tags: str = "") -> dict:
        """Manually add knowledge to the second brain."""
        return _post("api/brain/note", {"title": title, "content": content, "tags": tags})

    return mcp


def main():
    global _server
    parser = argparse.ArgumentParser(description="Synapse MCP server")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Synapse HTTP server URL")
    args = parser.parse_args()
    _server = args.server

    logger.info(f"starting Synapse MCP server -> {_server}")
    health = _get("health")
    if isinstance(health, dict) and health.get("status") == "ok":
        logger.info("connected to Synapse server")
    else:
        logger.warning("Synapse server not reachable yet; tools will fail until it is up")

    mcp = build_mcp()
    mcp.run()


if __name__ == "__main__":
    main()
