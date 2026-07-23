"""Memory tests use our OWN process - safe, no external targets needed."""
import ctypes
import os

import pytest

from synapse.re import memory


@pytest.fixture()
def own_handle():
    handle = memory.open_process(os.getpid())
    yield handle
    memory.close_handle(handle)


def test_open_own_process():
    handle = memory.open_process(os.getpid())
    assert handle
    memory.close_handle(handle)


def test_open_invalid_pid():
    with pytest.raises(memory.MemoryError_):
        memory.open_process(99999999)


def test_read_own_memory(own_handle):
    # allocate a buffer in our own process, plant a marker, read it back
    marker = b"SYNAPSE_MARKER_12345678"
    buf = ctypes.create_string_buffer(marker, len(marker))
    addr = ctypes.addressof(buf)
    data = memory.read_bytes(own_handle, addr, len(marker))
    assert data == marker


def test_write_own_memory():
    handle = memory.open_process(os.getpid(), write=True)
    try:
        buf = ctypes.create_string_buffer(b"\x00" * 16, 16)
        addr = ctypes.addressof(buf)
        written = memory.write_bytes(handle, addr, b"\xAA" * 16)
        assert written == 16
        assert buf.raw == b"\xAA" * 16
    finally:
        memory.close_handle(handle)


def test_regions_enumeration(own_handle):
    regs = memory.regions(own_handle)
    assert len(regs) > 0
    for r in regs[:10]:
        assert r["base"] > 0
        assert r["size"] > 0
        assert r["protect"] in memory.PROTECT_NAMES.values()


def test_pattern_parse():
    pat, mask = memory.parse_pattern("48 8B ?? 0F")
    assert pat == bytes([0x48, 0x8B, 0x00, 0x0F])
    assert mask == bytes([1, 1, 0, 1])
    pat2, mask2 = memory.parse_pattern("DEADbeef")
    assert pat2 == bytes([0xDE, 0xAD, 0xBE, 0xEF])
    assert mask2 == bytes([1, 1, 1, 1])
    with pytest.raises(memory.MemoryError_):
        memory.parse_pattern("XYZ")


def test_find_in_buffer():
    buf = b"\x00\x01\x48\x8B\xFF\x0F\x99\x48\x8B\x12\x0F\x00"
    pat, mask = memory.parse_pattern("48 8B ?? 0F")
    hits = memory.find_in_buffer(buf, pat, mask)
    assert hits == [2, 7]


def test_scan_own_process(own_handle):
    # plant a unique byte sequence in our own memory and find it via scan
    needle = b"\x53\x59\x4E\x41\x50\x53\x45\xBE\xEF\x00\x11\x22"
    buf = ctypes.create_string_buffer(needle, len(needle))
    addr = ctypes.addressof(buf)
    pattern = " ".join(f"{b:02X}" for b in needle)
    matches = memory.scan(own_handle, pattern, limit=10)
    assert addr in matches


def test_hexdump():
    dump = memory.hexdump(b"Hello World!!!!!" + b"\x00" * 16, base=0x1000)
    assert "00001000" in dump
    assert "Hello" in dump
