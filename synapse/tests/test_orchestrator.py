import asyncio

import pytest

from synapse.council import Orchestrator


class FakeClient:
    """Streams a canned answer token by token, records messages."""

    def __init__(self, answers=None, fail_for=None):
        self.answers = answers or {}
        self.fail_for = fail_for or set()
        self.calls = []

    async def chat_stream(self, model, messages, temperature=0.7, max_tokens=None):
        self.calls.append({"model": model, "messages": messages, "temperature": temperature})
        if model in self.fail_for:
            raise RuntimeError(f"boom from {model}")
        text = self.answers.get(model, f"answer-from-{model}")
        for tok in text.split(" "):
            yield tok + " ", {"total_tokens": 10}

    async def chat(self, model, messages, temperature=0.7, max_tokens=None):
        self.calls.append({"model": model, "messages": messages})
        return {"text": self.answers.get(model, f"answer-from-{model}"), "usage": {}}


def make_resolver(client):
    return lambda model_id: (client, model_id)


async def collect(gen):
    events = []
    async for evt in gen:
        events.append(evt)
    return events


@pytest.mark.asyncio
async def test_broadcast_parallel():
    client = FakeClient()
    orch = Orchestrator(make_resolver(client))
    events = await collect(
        orch.run("broadcast", "q?", ["m1", "m2", "m3"], extra_context="")
    )
    types = [e["type"] for e in events]
    assert "done" in types
    done = [e for e in events if e["type"] == "done"][0]
    assert set(done["result"]["answers"].keys()) == {"m1", "m2", "m3"}
    # every model produced model_done in stage answers
    done_models = {e["model"] for e in events if e["type"] == "model_done"}
    assert done_models == {"m1", "m2", "m3"}


@pytest.mark.asyncio
async def test_council_three_stages():
    client = FakeClient()
    orch = Orchestrator(make_resolver(client))
    events = await collect(
        orch.run("council", "hard question", ["m1", "m2"], chairman="m1")
    )
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert "answers" in stages
    assert "critiques" in stages
    assert "synthesis" in stages
    done = [e for e in events if e["type"] == "done"][0]
    assert done["result"]["synthesis"]
    assert done["result"]["chairman"] == "m1"


@pytest.mark.asyncio
async def test_council_chairman_fallback():
    """If the chosen chairman isn't in the answer set, pick the first survivor."""
    client = FakeClient()
    orch = Orchestrator(make_resolver(client))
    events = await collect(
        orch.run("council", "q", ["m1", "m2"], chairman="not-a-model")
    )
    done = [e for e in events if e["type"] == "done"][0]
    assert done["result"]["chairman"] in ("m1", "m2")


@pytest.mark.asyncio
async def test_council_handles_model_failure():
    client = FakeClient(fail_for={"m2"})
    orch = Orchestrator(make_resolver(client))
    events = await collect(
        orch.run("council", "q", ["m1", "m2"], chairman="m1")
    )
    errors = [e for e in events if e["type"] == "error"]
    assert any(e.get("model") == "m2" for e in errors)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["result"]["answers"].get("m1")  # m1 survived


@pytest.mark.asyncio
async def test_pipeline_chains_context():
    client = FakeClient()
    orch = Orchestrator(make_resolver(client))
    events = await collect(
        orch.run("pipeline", "build X", [], pipeline=["stepA", "stepB", "stepC"])
    )
    done = [e for e in events if e["type"] == "done"][0]
    assert len(done["result"]["steps"]) == 3
    assert done["result"]["final"]
    # step 2 should have received step 1's output in its prompt
    step_b_call = [c for c in client.calls if c["model"] == "stepB"][0]
    user_msg = step_b_call["messages"][-1]["content"]
    assert "answer-from-stepA" in user_msg


@pytest.mark.asyncio
async def test_unknown_mode():
    client = FakeClient()
    orch = Orchestrator(make_resolver(client))
    events = await collect(orch.run("nope", "q", ["m1"]))
    assert any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_brain_context_injected():
    client = FakeClient()
    orch = Orchestrator(make_resolver(client))
    await collect(orch.run("broadcast", "q", ["m1"], extra_context="secret-knowledge-42"))
    sys_msg = client.calls[0]["messages"][0]["content"]
    assert "secret-knowledge-42" in sys_msg
