import pytest

from synapse.brain import Brain, learn_from_text


def test_add_and_search(tmp_brain):
    tmp_brain.add("Windows PE structure", "PE files start with MZ header; .text holds code.", kind="fact", tags="re,pe")
    tmp_brain.add("Ollama default port", "Ollama serves OpenAI-compatible API on localhost:11434/v1", kind="fact", tags="local,llm")
    hits = tmp_brain.search("MZ header")
    assert hits
    assert any("PE" in h["title"] for h in hits)
    hits2 = tmp_brain.search("11434")
    assert hits2


def test_dedupe_merges(tmp_brain):
    tmp_brain.add("Fact A", "first version", kind="fact")
    tmp_brain.add("Fact A", "second version with more detail", kind="fact")
    entries = tmp_brain.list()
    assert len(entries) == 1
    assert "second version" in entries[0]["content"]  # merged, not duplicated


def test_stats_growth(tmp_brain):
    assert tmp_brain.stats()["total"] == 0
    tmp_brain.add("x", "y", kind="lesson")
    tmp_brain.add("z", "w", kind="analysis")
    stats = tmp_brain.stats()
    assert stats["total"] == 2
    assert stats["by_kind"]["lesson"] == 1
    assert stats["by_kind"]["analysis"] == 1


def test_delete(tmp_brain):
    e = tmp_brain.add("del me", "gone", kind="note")
    assert tmp_brain.delete(e["id"]) is True
    assert tmp_brain.search("gone") == []


def test_context_snippets(tmp_brain):
    tmp_brain.add("capstone usage", "capstone.Cs(arch, mode) then md.disasm(code, base)", kind="fact")
    ctx = tmp_brain.context_snippets("how to disassemble with capstone")
    assert "capstone" in ctx


def test_validation(tmp_brain):
    with pytest.raises(ValueError):
        tmp_brain.add("", "content")
    with pytest.raises(ValueError):
        tmp_brain.add("title", "")


@pytest.mark.asyncio
async def test_learner_with_fake_llm(tmp_brain):
    class FakeLLM:
        async def chat(self, model, messages, temperature=0.7, max_tokens=None):
            return {
                "text": '[{"title": "Pattern scan wildcard", "content": "Use ?? as wildcard byte in memory patterns like 48 8B ?? 00", "tags": "re,memory"}]',
                "usage": {},
            }

    added = await learn_from_text(
        tmp_brain,
        "We scanned memory with pattern 48 8B ?? 00 and found 3 hits. " * 5,
        source="test",
        client=FakeLLM(),
        model="fake",
    )
    assert added
    assert tmp_brain.stats()["total"] == 1
    assert "wildcard" in tmp_brain.list()[0]["title"]


@pytest.mark.asyncio
async def test_learner_fallback_heuristic(tmp_brain):
    """With no LLM, heuristic extractor still learns something from bullets."""
    material = "Session notes:\n- Memory scan found the health value at 0x7FF1234, it was a float of size 4 bytes\n- Second useful bullet point about the target process layout and modules\n"
    added = await learn_from_text(tmp_brain, material, source="test", client=None)
    assert added  # heuristic picked up the bullets
