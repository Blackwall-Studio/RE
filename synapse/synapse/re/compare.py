"""Broad comparison of multiple analysis targets.

Computes pairwise similarity (strings, imports, function counts, identity)
plus an optional LLM-written broad comparison summary.
"""
from __future__ import annotations

import itertools


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def compare_pair(a: dict, b: dict) -> dict:
    sa, sb = set(a.get("strings", [])), set(b.get("strings", []))
    ia = set(a.get("pe", {}).get("imports", {}).keys())
    ib = set(b.get("pe", {}).get("imports", {}).keys())
    ea = set(a.get("pe", {}).get("exports", []))
    eb = set(b.get("pe", {}).get("exports", []))

    a_funcs = {f.get("addr") for f in a.get("functions", [])}
    b_funcs = {f.get("addr") for f in b.get("functions", [])}

    return {
        "a": a.get("target"),
        "b": b.get("target"),
        "identical": a.get("sha256") == b.get("sha256"),
        "string_similarity": round(_jaccard(sa, sb), 3),
        "import_dll_similarity": round(_jaccard(ia, ib), 3),
        "export_similarity": round(_jaccard(ea, eb), 3),
        "function_count": {"a": len(a.get("functions", [])), "b": len(b.get("functions", []))},
        "shared_strings": len(sa & sb),
        "only_in_a": sorted(sa - sb)[:30],
        "only_in_b": sorted(sb - sa)[:30],
        "shared_import_dlls": sorted(ia & ib),
        "size_delta_bytes": (a.get("size_bytes", 0) - b.get("size_bytes", 0)),
    }


def compare_many(analyses: list[dict]) -> dict:
    pairs = [
        compare_pair(a, b) for a, b in itertools.combinations(analyses, 2)
    ]
    summary = {
        "targets": [a.get("target") for a in analyses],
        "count": len(analyses),
        "pairs": pairs,
        "all_identical": len({a.get("sha256") for a in analyses}) == 1,
        "avg_string_similarity": (
            round(sum(p["string_similarity"] for p in pairs) / len(pairs), 3) if pairs else 1.0
        ),
    }
    return summary


COMPARE_PROMPT = """You are a reverse engineering assistant. Below are comparison metrics for {count} binaries/process dumps:
{metrics}

Write a broad comparative analysis:
- Are these likely the same software / versions / variants? Why?
- Notable differences in imports, strings, size, structure
- Anything that looks packed, patched, or tampered with
- What to investigate next
Be concrete and technical. Under 250 words."""


async def llm_compare_summary(client, model: str, comparison: dict) -> str:
    metrics = []
    for p in comparison["pairs"]:
        metrics.append(
            f"- {p['a']} vs {p['b']}: identical={p['identical']}, "
            f"str_sim={p['string_similarity']}, dll_sim={p['import_dll_similarity']}, "
            f"funcs={p['function_count']}, shared_strings={p['shared_strings']}"
        )
    resp = await client.chat(
        model,
        [
            {
                "role": "user",
                "content": COMPARE_PROMPT.format(
                    count=comparison["count"], metrics="\n".join(metrics)[:5000]
                ),
            }
        ],
        temperature=0.3,
        max_tokens=800,
    )
    return resp["text"].strip()
