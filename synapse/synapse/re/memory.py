"""Windows process memory access via WinAPI (ctypes).

Attach to a live process by PID, read/write memory, enumerate committed
regions, and pattern-scan (Cheat Engine style "48 8B ?? 0F" patterns).

Reading other processes requires appropriate privileges (run as admin for
system/other-user processes; protected/anti-cheat processes will still
refuse access).
"""
from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64-bit correctness: HANDLE-returning functions must not use the default
# (32-bit) int restype, or handles get truncated.
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

READABLE_PROTECTS = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}  # R, RW, WC, XR, XRW, XWC

PROTECT_NAMES = {
    0x01: "NOACCESS",
    0x02: "R",
    0x04: "RW",
    0x08: "WC",
    0x10: "X",
    0x20: "XR",
    0x40: "XRW",
    0x80: "XWC",
}

TYPE_NAMES = {0x1000000: "IMAGE", 0x40000: "MAPPED", 0x20000: "PRIVATE"}


class MemoryError_(Exception):
    """Memory operation failed."""


DEFAULT_MAX_READ_BYTES = 512 * 1024 * 1024  # 512 MB; modern Windows modules clear 100 MB


def _max_read_bytes() -> int:
    """Resolve the per-read cap, overridable via ``SYNAPSE_RE_MAX_READ_BYTES``.

    Default is 512 MB (Electron-style apps / big games / IDEs routinely exceed
    100 MB for a single module). Set the env var to a higher value for very
    large binaries; or lower for hermetic tests.
    """
    raw = os.environ.get("SYNAPSE_RE_MAX_READ_BYTES", "").strip()
    if raw:
        try:
            return max(1024 * 1024, int(raw))  # at least 1 MB floor
        except ValueError:
            pass
    return DEFAULT_MAX_READ_BYTES


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def open_process(pid: int, write: bool = False) -> int:
    """Open a process handle for reading (and optionally writing)."""
    access = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    if write:
        access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        raise MemoryError_(
            f"OpenProcess failed for pid {pid} (win error {err}). "
            "Try running as administrator; protected/anti-cheat processes refuse access."
        )
    return handle


def close_handle(handle: int) -> None:
    if handle:
        kernel32.CloseHandle(handle)


def read_bytes(handle: int, address: int, size: int) -> bytes:
    """Read size bytes at address. Returns fewer bytes if the tail is unreadable."""
    cap = _max_read_bytes()
    if size <= 0 or size > cap:
        raise MemoryError_(
            f"invalid read size: {size} (max {cap // (1024 * 1024)} MB; "
            f"set SYNAPSE_RE_MAX_READ_BYTES env var to override the limit)"
        )
    buf = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buf, size, ctypes.byref(bytes_read)
    )
    if not ok and bytes_read.value == 0:
        err = ctypes.get_last_error()
        raise MemoryError_(f"ReadProcessMemory failed at 0x{address:X} (win error {err})")
    return buf.raw[: bytes_read.value]


def write_bytes(handle: int, address: int, data: bytes) -> int:
    """Write data at address (byte patching / memory injection). Returns bytes written."""
    if not data:
        raise MemoryError_("nothing to write")
    buf = ctypes.create_string_buffer(data, len(data))
    written = ctypes.c_size_t(0)
    ok = kernel32.WriteProcessMemory(
        handle, ctypes.c_void_p(address), buf, len(data), ctypes.byref(written)
    )
    if not ok:
        err = ctypes.get_last_error()
        raise MemoryError_(
            f"WriteProcessMemory failed at 0x{address:X} (win error {err}). "
            "Target page may be read-only or process protected."
        )
    return written.value


def regions(handle: int, max_regions: int = 20000) -> list[dict]:
    """Enumerate committed, readable memory regions of the process."""
    out: list[dict] = []
    mbi = MEMORY_BASIC_INFORMATION()
    address = 0
    while address < 0x7FFFFFFFFFFF and len(out) < max_regions:
        result = kernel32.VirtualQueryEx(
            handle,
            ctypes.c_void_p(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi),
        )
        if result == 0:
            break
        # ctypes c_void_p converts 0 to None - normalize back to ints
        r_base = mbi.BaseAddress or 0
        r_size = mbi.RegionSize or 0
        if (
            mbi.State == MEM_COMMIT
            and mbi.Protect in READABLE_PROTECTS
            and not (mbi.Protect & PAGE_GUARD)
        ):
            out.append(
                {
                    "base": r_base,
                    "size": r_size,
                    "protect": PROTECT_NAMES.get(mbi.Protect, hex(mbi.Protect)),
                    "type": TYPE_NAMES.get(mbi.Type, hex(mbi.Type)),
                }
            )
        next_addr = r_base + r_size
        if next_addr <= address:
            break
        address = next_addr
    return out


# ------------------------------------------------------------ pattern scan
def parse_pattern(pattern: str) -> tuple[bytes, bytes]:
    """Parse '48 8B ?? 0F' or '488B??0F' into (bytes, mask). mask 1 = must match."""
    cleaned = re.sub(r"[^0-9A-Fa-f?]", "", pattern)
    if len(cleaned) % 2 != 0:
        raise MemoryError_("pattern must have an even number of hex digits")
    pat = bytearray()
    mask = bytearray()
    for i in range(0, len(cleaned), 2):
        pair = cleaned[i : i + 2]
        if pair == "??":
            pat.append(0)
            mask.append(0)
        else:
            try:
                pat.append(int(pair, 16))
            except ValueError:
                raise MemoryError_(f"invalid pattern byte: {pair!r} (use ?? for wildcards)")
            mask.append(1)
    if not pat:
        raise MemoryError_("empty pattern")
    return bytes(pat), bytes(mask)


def find_in_buffer(buf: bytes, pat: bytes, mask: bytes, limit: int = 100) -> list[int]:
    """Find all masked-pattern matches in buf. Returns offsets."""
    hits: list[int] = []
    n = len(pat)
    # anchor on the first significant byte for speed
    first_sig = next((i for i, m in enumerate(mask) if m), None)
    if first_sig is None:
        return hits
    anchor = pat[first_sig]
    start = 0
    while len(hits) < limit:
        idx = buf.find(bytes([anchor]), start)
        if idx == -1:
            break
        cand = idx - first_sig
        if cand >= 0 and cand + n <= len(buf):
            if all(buf[cand + i] == pat[i] for i in range(n) if mask[i]):
                hits.append(cand)
        start = idx + 1
    return hits


def scan(
    handle: int,
    pattern: str,
    limit: int = 100,
    chunk_size: int = 1024 * 1024,
) -> list[int]:
    """Scan all readable committed regions for pattern. Returns absolute addresses."""
    pat, mask = parse_pattern(pattern)
    overlap = len(pat) - 1
    matches: list[int] = []
    for region in regions(handle):
        if len(matches) >= limit:
            break
        base, size = region["base"], region["size"]
        offset = 0
        carry = b""
        while offset < size and len(matches) < limit:
            to_read = min(chunk_size, size - offset)
            try:
                data = read_bytes(handle, base + offset, to_read)
            except MemoryError_:
                break
            buf = carry + data
            for hit in find_in_buffer(buf, pat, mask, limit - len(matches)):
                addr = base + offset - len(carry) + hit
                matches.append(addr)
            carry = buf[-overlap:] if overlap else b""
            offset += to_read
    return sorted(set(matches))


def hexdump(data: bytes, base: int = 0, width: int = 16) -> str:
    """Classic hexdump: offset, hex bytes, ascii gutter."""
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        hex_part = hex_part[: 8 * 3] + " " + hex_part[8 * 3 :] if len(chunk) > 8 else hex_part
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:08X}  {hex_part:<49}  {ascii_part}")
    return "\n".join(lines)
