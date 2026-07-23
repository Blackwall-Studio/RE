"""Live process introspection: list processes, modules, threads, memory
regions, and per-process network connections (the "networking stack logic").

Uses psutil where convenient and WinAPI (Toolhelp) for module enumeration.
Deep inspection of other users'/system processes requires admin.
"""
from __future__ import annotations

import ctypes
import socket
from ctypes import wintypes

import psutil

from . import memory

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


def _addr_to_str(laddr) -> str:
    if not laddr:
        return ""
    try:
        return f"{laddr.ip}:{laddr.port}"
    except AttributeError:
        return str(laddr)


def list_processes() -> list[dict]:
    """All running processes with memory usage and connection counts."""
    conn_count: dict[int, int] = {}
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.pid:
                conn_count[c.pid] = conn_count.get(c.pid, 0) + 1
    except (psutil.AccessDenied, PermissionError):
        pass

    out = []
    for p in psutil.process_iter(
        ["pid", "name", "exe", "username", "memory_info", "status"]
    ):
        try:
            i = p.info
            rss = i["memory_info"].rss if i.get("memory_info") else 0
            out.append(
                {
                    "pid": i["pid"],
                    "name": i.get("name") or "",
                    "exe": i.get("exe") or "",
                    "user": (i.get("username") or "").split("\\")[-1],
                    "mem_mb": round(rss / 1e6, 1),
                    "connections": conn_count.get(i["pid"], 0),
                    "status": i.get("status") or "",
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return sorted(out, key=lambda x: -x["mem_mb"])


def list_modules(pid: int, limit: int = 400) -> list[dict]:
    """Loaded modules (exe + DLLs) with base address and size."""
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if snapshot == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateToolhelp32Snapshot failed for pid {pid} (are you admin?)")
    mods: list[dict] = []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        has = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while has and len(mods) < limit:
            mods.append(
                {
                    "name": entry.szModule,
                    "base": entry.modBaseAddr or 0,
                    "size": entry.modBaseSize,
                    "path": entry.szExePath,
                }
            )
            has = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return mods


def _connections_for(pid: int) -> list[dict]:
    conns: list[dict] = []
    try:
        for c in psutil.Process(pid).net_connections(kind="all"):
            proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
            family = "v4" if c.family == socket.AF_INET else "v6"
            conns.append(
                {
                    "proto": proto,
                    "family": family,
                    "laddr": _addr_to_str(c.laddr),
                    "raddr": _addr_to_str(c.raddr),
                    "status": c.status or "",
                }
            )
    except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError) as e:
        conns.append({"error": f"connection list requires privileges: {e}"})
    return conns


def process_detail(pid: int, max_regions_listed: int = 60) -> dict:
    """Deep inspection: info, modules, threads, network connections, memory map."""
    p = psutil.Process(pid)
    with p.oneshot():
        info = {
            "pid": pid,
            "name": p.name(),
            "exe": p.exe() if _safe(p.exe) else "",
            "cmdline": " ".join(_safe(p.cmdline) or []),
            "user": _safe(p.username) or "",
            "status": p.status(),
            "created": p.create_time(),
            "mem_rss_mb": round(p.memory_info().rss / 1e6, 1),
            "mem_vms_mb": round(p.memory_info().vms / 1e6, 1),
            "num_threads": p.num_threads(),
            "cpu_percent": p.cpu_percent(interval=0.05),
        }
        threads = [
            {"id": t.id, "user_time": round(t.user_time, 3), "system_time": round(t.system_time, 3)}
            for t in p.threads()
        ]

    try:
        modules = list_modules(pid)
    except OSError as e:
        modules = [{"error": str(e)}]

    conns = _connections_for(pid)

    region_summary: dict = {"count": 0, "by_protect": {}, "by_type": {}, "top": []}
    try:
        handle = memory.open_process(pid)
        try:
            regs = memory.regions(handle, max_regions=5000)
            region_summary["count"] = len(regs)
            for r in regs:
                region_summary["by_protect"][r["protect"]] = (
                    region_summary["by_protect"].get(r["protect"], 0) + 1
                )
                region_summary["by_type"][r["type"]] = (
                    region_summary["by_type"].get(r["type"], 0) + 1
                )
            region_summary["top"] = [
                {"base": f"0x{r['base']:X}", "size": r["size"], "protect": r["protect"], "type": r["type"]}
                for r in sorted(regs, key=lambda x: -x["size"])[:max_regions_listed]
            ]
        finally:
            memory.close_handle(handle)
    except memory.MemoryError_ as e:
        region_summary["error"] = str(e)

    return {
        "info": info,
        "modules": [
            {**m, "base": f"0x{m['base']:X}"} if "base" in m else m for m in modules
        ],
        "threads": threads,
        "connections": conns,
        "regions": region_summary,
    }


def _safe(fn, default=None):
    try:
        return fn()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return default
