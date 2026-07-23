import pytest

from synapse.re import compare


def make_analysis(target, sha, strings, imports, exports, nfuncs):
    return {
        "target": target,
        "sha256": sha,
        "size_bytes": 1000,
        "strings": strings,
        "pe": {"imports": {d: ["f"] for d in imports}, "exports": exports},
        "functions": [{"addr": i * 16} for i in range(nfuncs)],
    }


def test_compare_identical():
    a = make_analysis("a.exe", "abc", ["s1", "s2"], ["KERNEL32.dll"], ["main"], 10)
    b = make_analysis("b.exe", "abc", ["s1", "s2"], ["KERNEL32.dll"], ["main"], 10)
    result = compare.compare_many([a, b])
    assert result["all_identical"] is True
    pair = result["pairs"][0]
    assert pair["string_similarity"] == 1.0
    assert pair["identical"] is True


def test_compare_different():
    a = make_analysis("a.exe", "aaa", ["shared", "only-a"], ["KERNEL32.dll", "WS2_32.dll"], ["main"], 10)
    b = make_analysis("b.exe", "bbb", ["shared", "only-b"], ["KERNEL32.dll"], ["main", "run"], 5)
    result = compare.compare_many([a, b])
    assert result["all_identical"] is False
    pair = result["pairs"][0]
    assert 0 < pair["string_similarity"] < 1
    assert "only-a" in pair["only_in_a"]
    assert "only-b" in pair["only_in_b"]
    assert "KERNEL32.dll" in pair["shared_import_dlls"]
    assert pair["function_count"]["a"] == 10
    assert pair["function_count"]["b"] == 5


def test_compare_three_targets():
    a = make_analysis("a", "1", ["x"], ["K"], [], 3)
    b = make_analysis("b", "2", ["x"], ["K"], [], 3)
    c = make_analysis("c", "3", ["y"], ["W"], [], 3)
    result = compare.compare_many([a, b, c])
    assert result["count"] == 3
    assert len(result["pairs"]) == 3  # C(3,2)


@pytest.mark.asyncio
async def test_llm_compare_summary():
    class FakeClient:
        async def chat(self, model, messages, temperature=0.7, max_tokens=None):
            return {"text": "These are two versions of the same binary.", "usage": {}}

    a = make_analysis("a", "1", ["x"], ["K"], [], 3)
    b = make_analysis("b", "2", ["x"], ["K"], [], 3)
    comparison = compare.compare_many([a, b])
    summary = await compare.llm_compare_summary(FakeClient(), "m", comparison)
    assert "versions" in summary
