"""Resilience layer (pattern ported from HexStrike's intelligent error
handling): error classification, retry with exponential backoff, and
fallback chains (alternative models when one fails).

Adapted for LLM calls instead of shell tools: if a model 429s/500s/times
out, retry with backoff; if it keeps failing, try the next model in the
fallback chain.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum


class ErrorType(Enum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    AUTH_FAILED = "auth_failed"
    MODEL_NOT_FOUND = "model_not_found"
    NETWORK = "network"
    UNKNOWN = "unknown"


_PATTERNS = [
    (r"429|rate.?limit|too many requests|throttl", ErrorType.RATE_LIMITED),
    (r"timeout|timed out|deadline", ErrorType.TIMEOUT),
    (r"401|403|unauthorized|forbidden|invalid api key|authentication", ErrorType.AUTH_FAILED),
    (r"404|model not found|no such model|does not exist", ErrorType.MODEL_NOT_FOUND),
    (r"5\d\d|internal server|bad gateway|service unavailable|overloaded", ErrorType.SERVER_ERROR),
    (r"connection|connect error|network|unreachable|reset", ErrorType.NETWORK),
]


def classify_error(message: str) -> ErrorType:
    text = (message or "").lower()
    for pattern, etype in _PATTERNS:
        if re.search(pattern, text):
            return etype
    return ErrorType.UNKNOWN


# Which error types are worth retrying (auth/not-found fail fast)
RETRYABLE = {ErrorType.TIMEOUT, ErrorType.RATE_LIMITED, ErrorType.SERVER_ERROR, ErrorType.NETWORK, ErrorType.UNKNOWN}


@dataclass
class RecoveryResult:
    text: str = ""
    model_used: str = ""
    attempts: int = 0
    fell_back_to: str | None = None
    errors: list[str] = field(default_factory=list)
    ok: bool = False


async def call_with_recovery(
    client,
    model: str,
    messages: list[dict],
    resolver,
    fallback_chain: list[str] | None = None,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    temperature: float = 0.7,
) -> RecoveryResult:
    """Call a model with retry+backoff, then walk the fallback chain.

    resolver(model_id) -> (client, real_model_id) for fallback candidates.
    """
    result = RecoveryResult(model_used=model)
    candidates = [(client, model)] + [
        (resolver(f) if resolver else (client, f), f) for f in (fallback_chain or [])
    ]

    for cand_idx, (cand_client, cand_model) in enumerate(candidates):
        if isinstance(cand_client, tuple):  # resolver returned (client, real_id)
            cand_client, cand_model = cand_client
        for attempt in range(1, max_attempts + 1):
            result.attempts += 1
            try:
                resp = await cand_client.chat(cand_model, messages, temperature)
                result.text = resp["text"]
                result.ok = True
                result.model_used = cand_model
                if cand_idx > 0:
                    result.fell_back_to = cand_model
                return result
            except Exception as e:
                msg = str(e)
                result.errors.append(f"{cand_model} attempt {attempt}: {msg[:200]}")
                etype = classify_error(msg)
                if etype not in RETRYABLE:
                    break  # fail fast to next candidate
                if attempt < max_attempts:
                    delay = base_delay * (2 ** (attempt - 1))
                    if etype == ErrorType.RATE_LIMITED:
                        delay *= 2
                    await asyncio.sleep(min(delay, 30.0))
    return result
