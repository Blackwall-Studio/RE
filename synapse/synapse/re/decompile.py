"""LLM-assisted decompilation: disassembly -> C-like pseudocode via any
configured model (prefer local = zero token cost).
"""
from __future__ import annotations

import asyncio

PSEUDO_PROMPT = """You are a reverse engineering assistant. Convert this x86/x64 disassembly into concise, readable C-like pseudocode.

Rules:
- Infer variable roles from usage (counters, pointers, flags)
- Keep it under 40 lines
- Add a one-line summary of what the function does at the top as a comment
- Note any called imports and likely purpose
- Output ONLY the pseudocode, no markdown fences

Disassembly:
{disasm}"""


async def decompile_functions(
    client,
    model: str,
    functions: list[dict],
    max_funcs: int = 12,
) -> list[dict]:
    """Attach 'pseudocode' to the top functions (most-called first)."""

    async def one(fn: dict) -> dict:
        out = dict(fn)
        try:
            resp = await client.chat(
                model,
                [{"role": "user", "content": PSEUDO_PROMPT.format(disasm=fn["disasm"][:6000])}],
                temperature=0.2,
                max_tokens=1500,
            )
            out["pseudocode"] = resp["text"].strip()
        except Exception as e:
            out["pseudocode"] = f"// decompile failed: {e}"
        return out

    top = functions[:max_funcs]
    done = await asyncio.gather(*[one(f) for f in top])
    # merge back with untouched tail
    return list(done) + functions[len(top):]
