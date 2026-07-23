"""Active learning: turns sessions and analyses into durable brain entries.

Prefers a LOCAL model for extraction so background learning costs zero
tokens. Falls back to a heuristic extractor when no LLM is available.
"""
from __future__ import annotations

import json
import re

from .store import Brain

EXTRACT_PROMPT = """You extract durable, reusable knowledge from AI work sessions.

From the material below, extract up to 5 facts/lessons that would help in
FUTURE sessions (technical insights, pitfalls, API details, RE findings,
decisions and why). Skip fluff, greetings, and one-off task details.

Return ONLY a JSON array, no markdown fences:
[{{"title": "short label", "content": "the fact/lesson in 1-3 sentences", "tags": "comma,separated"}}]

Material:
---
{material}
---"""


def _parse_facts(text: str) -> list[dict]:
    """Tolerant JSON-array extraction from model output."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("title") and item.get("content"):
            out.append(
                {
                    "title": str(item["title"])[:300],
                    "content": str(item["content"]),
                    "tags": str(item.get("tags", ""))[:200],
                }
            )
    return out[:5]


def _heuristic_facts(material: str) -> list[dict]:
    """Zero-LLM fallback: pull the densest bullet/numbered lines."""
    facts = []
    for line in material.splitlines():
        s = line.strip()
        if re.match(r"^([-*•]|\d+[.)])\s+\S", s) and 40 <= len(s) <= 400:
            facts.append({"title": s[:60], "content": s, "tags": "auto"})
        if len(facts) >= 5:
            break
    return facts


async def learn_from_text(
    brain: Brain,
    material: str,
    source: str,
    kind: str = "lesson",
    client=None,
    model: str | None = None,
) -> list[dict]:
    """Extract facts from material and grow the brain. Returns added entries."""
    material = (material or "").strip()
    if len(material) < 80:
        return []

    facts: list[dict] = []
    if client is not None and model:
        try:
            resp = await client.chat(
                model,
                [{"role": "user", "content": EXTRACT_PROMPT.format(material=material[:6000])}],
                temperature=0.2,
                max_tokens=1200,
            )
            facts = _parse_facts(resp["text"])
        except Exception:
            facts = []
    if not facts:
        facts = _heuristic_facts(material)

    added = []
    for f in facts:
        entry = brain.add(
            title=f["title"],
            content=f["content"],
            kind=kind,
            tags=f.get("tags", ""),
            source=source,
        )
        added.append({"title": entry["title"], "kind": kind, "merged": entry["merged"]})
    return added
