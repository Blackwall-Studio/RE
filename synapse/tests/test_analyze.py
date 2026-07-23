"""Analyze tests: build a tiny PE fixture, plus analyze the real python.exe."""
import sys
from pathlib import Path

import pytest

from synapse.re import analyze


def test_analyze_real_python_exe():
    """Analyze the interpreter binary itself - real PE, always present."""
    exe = sys.executable
    result = analyze.analyze_file(exe, max_functions=10, max_strings=50)
    assert result["sha256"]
    assert result["size_bytes"] > 0
    pe = result["pe"]
    assert pe["sections"]
    assert pe["imports"]  # python.exe imports something
    assert result["strings"]
    # sanity on machine arch
    assert pe["machine"] in ("0x8664", "0x14c")


def test_analyze_missing_file():
    with pytest.raises(FileNotFoundError):
        analyze.analyze_file("C:\\definitely\\not\\here\\nope.exe")


def test_analyze_non_pe(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"NOTAPE" * 100)
    with pytest.raises(ValueError):
        analyze.analyze_file(str(junk))


def test_extract_strings():
    data = b"\x00\x00Hello World From Synapse\x00\x00" + "Wide String Test".encode("utf-16-le") + b"\x00" * 8
    strings = analyze.extract_strings(data)
    assert any("Hello World" in s for s in strings)


def test_analyze_process_module_self():
    """Dump OUR OWN main module from live memory and analyze it - exercises
    the full live-process RE path safely."""
    import os
    result = analyze.analyze_process_module(os.getpid(), max_functions=5, max_strings=20)
    assert result["live"]["pid"] == os.getpid()
    assert result["sha256"]
    assert result["pe"]["sections"]
