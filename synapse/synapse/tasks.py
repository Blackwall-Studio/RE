"""Async task registry (pattern ported from HexStrike's ProcessManager).

Long-running jobs (analyze-with-LLM, big scans, council runs) register here
so the UI can list them, poll status, and cancel them.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any


class TaskRegistry:
    def __init__(self):
        self._counter = itertools.count(1)
        self._tasks: dict[int, dict] = {}

    def register(self, kind: str, label: str) -> int:
        task_id = next(self._counter)
        self._tasks[task_id] = {
            "id": task_id,
            "kind": kind,
            "label": label,
            "status": "running",
            "created": time.time(),
            "finished": None,
            "progress": "",
            "result": None,
            "error": None,
            "_asyncio_task": None,
        }
        return task_id

    def attach(self, task_id: int, asyncio_task: asyncio.Task) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["_asyncio_task"] = asyncio_task

    def progress(self, task_id: int, note: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["progress"] = note

    def complete(self, task_id: int, result: Any = None) -> None:
        if task_id in self._tasks:
            self._tasks[task_id].update(
                status="done", finished=time.time(), result=result
            )

    def fail(self, task_id: int, error: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id].update(
                status="error", finished=time.time(), error=error
            )

    def cancel(self, task_id: int) -> bool:
        t = self._tasks.get(task_id)
        if not t or t["status"] != "running":
            return False
        at = t.get("_asyncio_task")
        if at:
            at.cancel()
        t.update(status="cancelled", finished=time.time())
        return True

    def get(self, task_id: int) -> dict | None:
        t = self._tasks.get(task_id)
        if not t:
            return None
        return {k: v for k, v in t.items() if k != "_asyncio_task"}

    def list(self, limit: int = 50) -> list[dict]:
        tasks = sorted(self._tasks.values(), key=lambda t: -t["created"])[:limit]
        return [{k: v for k, v in t.items() if k != "_asyncio_task"} for t in tasks]

    def stats(self) -> dict:
        return {
            "total": len(self._tasks),
            "running": sum(1 for t in self._tasks.values() if t["status"] == "running"),
            "done": sum(1 for t in self._tasks.values() if t["status"] == "done"),
            "error": sum(1 for t in self._tasks.values() if t["status"] == "error"),
        }
