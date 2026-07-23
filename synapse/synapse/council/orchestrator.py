"""Multi-LLM orchestration engine.

Modes:
  broadcast - one prompt to all models in parallel, collect every answer.
  council   - stage 1: independent answers (parallel)
              stage 2: cross-critique (parallel, each model sees all answers)
              stage 3: chairman synthesizes the final answer.
  pipeline  - sequential chain: each model receives the previous model's output.

The orchestrator is an async generator of event dicts consumed by the
WebSocket layer. Models are addressed by opaque ids; a resolver callable maps
an id to (client, real_model_id) so cloud and local models mix freely.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Awaitable, Callable

# resolver(model_id) -> (client_with_chat_stream, real_model_id)
Resolver = Callable[[str], tuple[Any, str]]

SYSTEM_BASE = (
    "You are one expert in a council of AI models doing deep research and "
    "software engineering. Be precise, technical, and concrete. "
    "If code is requested, produce working code."
)

CRITIQUE_PROMPT = """Original task:
{prompt}

Here are the candidate answers from the council:
{answers}

Critique them ruthlessly and constructively:
- point out factual errors, bugs, security issues, and weak reasoning, per answer
- identify which answer is strongest and why
- list what is missing that the final answer must include
Be concise and specific."""

SYNTH_PROMPT = """Original task:
{prompt}

Council answers:
{answers}

Cross-critiques:
{critiques}

You are the chairman. Synthesize ONE final best answer:
- merge the strongest correct ideas, discard what was debunked
- be complete, concrete, and directly usable
- if code is involved, output the full final code
- end with a short "Confidence & caveats" section."""

PIPELINE_HANDOFF = """Original task:
{prompt}

Work so far (from previous step, by {prev_model}):
---
{prev_output}
---

You are step {index} in a pipeline. Improve/extend the work above toward the
original task. Do not restart from scratch unless it is fundamentally wrong.
Output your improved complete version."""


class Orchestrator:
    def __init__(self, resolver: Resolver):
        self._resolve = resolver

    # ------------------------------------------------------------------ API
    async def run(
        self,
        mode: str,
        prompt: str,
        models: list[str],
        chairman: str | None = None,
        pipeline: list[str] | None = None,
        extra_context: str = "",
    ) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue()
        result: dict = {}

        async def driver():
            try:
                if mode == "broadcast":
                    result.update(await self._broadcast(prompt, models, queue, extra_context))
                elif mode == "council":
                    result.update(
                        await self._council(prompt, models, chairman, queue, extra_context)
                    )
                elif mode == "pipeline":
                    result.update(
                        await self._pipeline(prompt, pipeline or models, queue, extra_context)
                    )
                else:
                    await queue.put({"type": "error", "message": f"unknown mode: {mode}"})
            except Exception as e:  # never let the driver die silently
                await queue.put({"type": "error", "message": f"orchestrator: {e}"})
            finally:
                await queue.put({"type": "_end"})

        task = asyncio.create_task(driver())
        try:
            while True:
                evt = await queue.get()
                if evt["type"] == "_end":
                    break
                yield evt
        finally:
            await task
        yield {"type": "done", "result": result}

    # -------------------------------------------------------------- helpers
    async def _call(
        self,
        model_id: str,
        messages: list[dict],
        queue: asyncio.Queue,
        stage: str,
        temperature: float = 0.7,
    ) -> tuple[str, str, str | None]:
        """Stream one model; returns (model_id, full_text, error)."""
        start = time.perf_counter()
        full: list[str] = []
        usage: dict = {}
        try:
            client, real_id = self._resolve(model_id)
            async for delta, usage in client.chat_stream(real_id, messages, temperature):
                full.append(delta)
                await queue.put(
                    {"type": "token", "model": model_id, "stage": stage, "text": delta}
                )
            text = "".join(full)
            await queue.put(
                {
                    "type": "model_done",
                    "model": model_id,
                    "stage": stage,
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                    "usage": usage,
                    "text": text,
                }
            )
            return model_id, text, None
        except Exception as e:
            await queue.put(
                {
                    "type": "error",
                    "model": model_id,
                    "stage": stage,
                    "message": str(e)[:500],
                }
            )
            return model_id, "", str(e)

    def _system(self, extra_context: str) -> str:
        if extra_context:
            return SYSTEM_BASE + "\n\nRelevant knowledge from prior sessions:\n" + extra_context
        return SYSTEM_BASE

    @staticmethod
    def _format_answers(answers: dict[str, str]) -> str:
        parts = []
        for i, (m, t) in enumerate(answers.items(), 1):
            parts.append(f"--- Answer {i} ({m}) ---\n{t}")
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- modes
    async def _broadcast(
        self, prompt: str, models: list[str], queue: asyncio.Queue, extra_context: str
    ) -> dict:
        await queue.put(
            {"type": "stage", "stage": "answers", "label": f"Broadcast to {len(models)} models"}
        )
        msgs = [
            {"role": "system", "content": self._system(extra_context)},
            {"role": "user", "content": prompt},
        ]
        results = await asyncio.gather(
            *[self._call(m, msgs, queue, "answers") for m in models]
        )
        answers = {m: t for m, t, err in results if not err}
        return {"answers": answers}

    async def _council(
        self,
        prompt: str,
        models: list[str],
        chairman: str | None,
        queue: asyncio.Queue,
        extra_context: str,
    ) -> dict:
        # Stage 1: independent answers
        await queue.put(
            {"type": "stage", "stage": "answers", "label": "Stage 1: Independent answers"}
        )
        msgs = [
            {"role": "system", "content": self._system(extra_context)},
            {"role": "user", "content": prompt},
        ]
        results = await asyncio.gather(
            *[self._call(m, msgs, queue, "answers") for m in models]
        )
        answers = {m: t for m, t, err in results if not err}
        if not answers:
            return {"answers": {}, "error": "all models failed in stage 1"}

        # Stage 2: cross-critique (skip if only one model survived)
        critiques: dict[str, str] = {}
        if len(answers) > 1:
            await queue.put(
                {"type": "stage", "stage": "critiques", "label": "Stage 2: Cross-critique"}
            )
            formatted = self._format_answers(answers)
            c_msgs = [
                {"role": "system", "content": SYSTEM_BASE},
                {
                    "role": "user",
                    "content": CRITIQUE_PROMPT.format(prompt=prompt, answers=formatted),
                },
            ]
            c_results = await asyncio.gather(
                *[self._call(m, c_msgs, queue, "critiques", 0.4) for m in answers]
            )
            critiques = {m: t for m, t, err in c_results if not err}

        # Stage 3: chairman synthesis
        chair = chairman if chairman in answers else next(iter(answers))
        await queue.put(
            {
                "type": "stage",
                "stage": "synthesis",
                "label": f"Stage 3: Synthesis by {chair}",
            }
        )
        s_msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {
                "role": "user",
                "content": SYNTH_PROMPT.format(
                    prompt=prompt,
                    answers=self._format_answers(answers),
                    critiques=self._format_answers(critiques) or "(none)",
                ),
            },
        ]
        _, synthesis, err = await self._call(chair, s_msgs, queue, "synthesis", 0.5)
        return {
            "answers": answers,
            "critiques": critiques,
            "chairman": chair,
            "synthesis": synthesis,
            **({"error": err} if err else {}),
        }

    async def _pipeline(
        self, prompt: str, chain: list[str], queue: asyncio.Queue, extra_context: str
    ) -> dict:
        steps: list[dict] = []
        prev_output = ""
        prev_model = "(start)"
        for i, model in enumerate(chain):
            stage = f"step_{i}"
            await queue.put(
                {
                    "type": "stage",
                    "stage": stage,
                    "label": f"Pipeline step {i + 1}/{len(chain)}: {model}",
                }
            )
            if i == 0:
                msgs = [
                    {"role": "system", "content": self._system(extra_context)},
                    {"role": "user", "content": prompt},
                ]
            else:
                msgs = [
                    {"role": "system", "content": SYSTEM_BASE},
                    {
                        "role": "user",
                        "content": PIPELINE_HANDOFF.format(
                            prompt=prompt,
                            prev_model=prev_model,
                            prev_output=prev_output,
                            index=i + 1,
                        ),
                    },
                ]
            _, text, err = await self._call(model, msgs, queue, stage)
            steps.append({"model": model, "output": text, **({"error": err} if err else {})})
            if err:
                return {"steps": steps, "final": prev_output, "error": f"step {i} failed"}
            prev_output, prev_model = text, model
        return {"steps": steps, "final": prev_output}
