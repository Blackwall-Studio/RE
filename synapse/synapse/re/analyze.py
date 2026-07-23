"""Static analysis: PE parsing, strings, disassembly, function discovery.

Works on files from disk OR bytes dumped from a live process (analyze_bytes).
Disassembly via capstone; function discovery via call-target heuristics.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import capstone
import pefile

from . import memory as mem

ASCII_RE = re.compile(rb"[\x20-\x7e]{5,}")
UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){5,}")

MACHINE_CAPSTONE = {
    0x014C: (capstone.CS_ARCH_X86, capstone.CS_MODE_32),
    0x8664: (capstone.CS_ARCH_X86, capstone.CS_MODE_64),
    0x01C4: (capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM),
    0xAA64: (capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM),
}

EXECUTABLE = 0x20000000  # IMAGE_SCN_MEM_EXECUTE


def extract_strings(data: bytes, limit: int = 600) -> list[str]:
    out: list[str] = []
    for m in ASCII_RE.finditer(data):
        out.append(m.group().decode("ascii", "replace"))
        if len(out) >= limit:
            return out
    for m in UTF16_RE.finditer(data):
        out.append(m.group().decode("utf-16-le", "replace"))
        if len(out) >= limit:
            return out
    return out


def parse_pe(data: bytes) -> dict:
    pe = pefile.PE(data=data, fast_load=False)
    machine = pe.FILE_HEADER.Machine
    sections = []
    for s in pe.sections:
        sections.append(
            {
                "name": s.Name.rstrip(b"\x00").decode("ascii", "replace"),
                "vaddr": s.VirtualAddress,
                "vsize": s.Misc_VirtualSize,
                "raw_size": s.SizeOfRawData,
                "entropy": round(s.get_entropy(), 2),
                "executable": bool(s.Characteristics & EXECUTABLE),
            }
        )

    imports: dict[str, list[str]] = {}
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            dll = entry.dll.decode("ascii", "replace")
            names = []
            for imp in entry.imports:
                names.append(
                    imp.name.decode("ascii", "replace") if imp.name else f"ord_{imp.ordinal}"
                )
            imports[dll] = names

    exports: list[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if exp.name:
                exports.append(exp.name.decode("ascii", "replace"))

    return {
        "machine": hex(machine),
        "image_base": pe.OPTIONAL_HEADER.ImageBase,
        "entry_point": pe.OPTIONAL_HEADER.ImageBase + pe.OPTIONAL_HEADER.AddressOfEntryPoint,
        "subsystem": pe.OPTIONAL_HEADER.Subsystem,
        "is_64": pe.PE_TYPE == pefile.OPTIONAL_HEADER_MAGIC_PE_PLUS,
        "sections": sections,
        "imports": imports,
        "exports": exports[:300],
        "import_count": sum(len(v) for v in imports.values()),
        "_pe": pe,  # internal, stripped before returning to API callers
    }


def _disasm_region(pe: pefile.PE, data: bytes, section: dict, max_insns: int = 20000) -> list:
    arch_mode = MACHINE_CAPSTONE.get(pe.FILE_HEADER.Machine, MACHINE_CAPSTONE[0x8664])
    md = capstone.Cs(*arch_mode)
    md.detail = False
    sec = next(
        s for s in pe.sections
        if s.VirtualAddress == section["vaddr"]
    )
    code = sec.get_data()[: section["raw_size"]]
    base = pe.OPTIONAL_HEADER.ImageBase + section["vaddr"]
    insns = []
    for insn in md.disasm(code, base):
        insns.append(insn)
        if len(insns) >= max_insns:
            break
    return insns


def discover_functions(insns: list, max_functions: int = 60) -> list[dict]:
    """Function starts = call targets + entry; sized by next call target or ret."""
    targets: dict[int, int] = {}  # addr -> inbound call count
    for insn in insns:
        if insn.mnemonic.startswith("call"):
            m = re.match(r"0x[0-9a-fA-F]+", insn.op_str)
            if m:
                t = int(m.group(0), 16)
                targets[t] = targets.get(t, 0) + 1

    if insns:
        targets.setdefault(insns[0].address, 0)

    addr_to_insn = {i.address: i for i in insns}
    ordered = sorted(targets)
    functions = []
    for idx, addr in enumerate(ordered):
        if addr not in addr_to_insn:
            continue
        # size: until next known target or a ret, capped
        end_bound = ordered[idx + 1] if idx + 1 < len(ordered) else None
        fn_insns = []
        cur = addr
        while len(fn_insns) < 400:
            insn = addr_to_insn.get(cur)
            if insn is None:
                break
            fn_insns.append(insn)
            cur += insn.size
            if end_bound and cur >= end_bound:
                break
            if insn.mnemonic == "ret" and len(fn_insns) >= 3:
                break
        if len(fn_insns) < 2:
            continue
        functions.append(
            {
                "addr": addr,
                "size": sum(i.size for i in fn_insns),
                "instrs": len(fn_insns),
                "calls": targets.get(addr, 0),
                "disasm": "\n".join(f"0x{i.address:X}  {i.mnemonic}  {i.op_str}" for i in fn_insns),
            }
        )

    functions.sort(key=lambda f: -f["calls"])
    return functions[:max_functions]


def analyze_bytes(data: bytes, target_name: str, max_functions: int = 60, max_strings: int = 600) -> dict:
    """Full static analysis of a PE image from raw bytes."""
    info = parse_pe(data)
    pe = info.pop("_pe")
    sha = hashlib.sha256(data).hexdigest()

    functions: list[dict] = []
    exec_sections = [s for s in info["sections"] if s["executable"]]
    for section in exec_sections[:2]:  # usually just .text
        try:
            insns = _disasm_region(pe, data, section)
            functions = discover_functions(insns, max_functions)
            if functions:
                break
        except Exception:
            continue

    return {
        "target": target_name,
        "sha256": sha,
        "size_bytes": len(data),
        "pe": info,
        "functions": functions,
        "function_count": len(functions),
        "strings": extract_strings(data, max_strings),
    }


def analyze_file(path: str, max_functions: int = 60, max_strings: int = 600) -> dict:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {path}")
        raise FileNotFoundError(path)
    data = p.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError(f"not a PE file (missing MZ header): {path}")
    return analyze_bytes(data, str(p), max_functions, max_strings)


def remap_dump(dump: bytes) -> bytes:
    """Rebuild a file-layout PE from a live memory dump.

    In memory, sections live at their VirtualAddress; in the file they live at
    PointerToRawData. Import/export parsing needs file layout, so we copy each
    section back to its raw offset. Headers (page 0 of the dump) are kept.
    """
    try:
        pe = pefile.PE(data=dump, fast_load=True)
        headers_size = pe.OPTIONAL_HEADER.SizeOfHeaders
        out = bytearray(dump[:headers_size])
        for s in pe.sections:
            raw_end = s.PointerToRawData + s.SizeOfRawData
            if len(out) < raw_end:
                out.extend(b"\x00" * (raw_end - len(out)))
            src = dump[s.VirtualAddress : s.VirtualAddress + s.SizeOfRawData]
            out[s.PointerToRawData : s.PointerToRawData + len(src)] = src
        return bytes(out)
    except Exception:
        return dump  # best effort: return as-is


def analyze_process_module(pid: int, module_base: int | None = None, max_functions: int = 60, max_strings: int = 600) -> dict:
    """Dump the main module (or a chosen module) from a LIVE process and analyze it."""
    from .processes import list_modules

    handle = mem.open_process(pid)
    try:
        mods = list_modules(pid)
        if not mods or "error" in mods[0]:
            raise RuntimeError(f"could not enumerate modules for pid {pid}")
        chosen = None
        if module_base is not None:
            chosen = next((m for m in mods if m.get("base") == module_base), None)
            if chosen is None:
                raise ValueError(f"module base 0x{module_base:X} not found in pid {pid}")
        else:
            chosen = mods[0]  # main executable module
        dump = mem.read_bytes(handle, chosen["base"], chosen["size"])
    finally:
        mem.close_handle(handle)

    result = analyze_bytes(remap_dump(dump), f"pid {pid}: {chosen['name']} (live dump)", max_functions, max_strings)
    result["sha256"] = hashlib.sha256(dump).hexdigest()  # hash the real dump
    result["live"] = {"pid": pid, "module": chosen["name"], "base": f"0x{chosen['base']:X}"}
    result["note"] = "live dump remapped to file layout; sha256 is over the raw dump"
    return result
