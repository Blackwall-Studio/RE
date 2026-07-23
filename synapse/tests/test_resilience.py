import pytest

from synapse.resilience import ErrorType, call_with_recovery, classify_error


def test_classify():
    assert classify_error("HTTP 429 too many requests") == ErrorType.RATE_LIMITED
    assert classify_error("request timed out") == ErrorType.TIMEOUT
    assert classify_error("401 unauthorized") == ErrorType.AUTH_FAILED
    assert classify_error("500 internal server error") == ErrorType.SERVER_ERROR
    assert classify_error("model not found") == ErrorType.MODEL_NOT_FOUND
    assert classify_error("something weird") == ErrorType.UNKNOWN


class FlakyClient:
    def __init__(self, fail_times, error="500 internal server error"):
        self.fail_times = fail_times
        self.calls = 0
        self.error = error

    async def chat(self, model, messages, temperature=0.7, max_tokens=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(self.error)
        return {"text": f"ok-{model}", "usage": {}}


@pytest.mark.asyncio
async def test_retry_succeeds_after_backoff():
    client = FlakyClient(fail_times=2)
    result = await call_with_recovery(client, "m", [{"role": "user", "content": "hi"}], None, base_delay=0.01)
    assert result.ok is True
    assert result.text == "ok-m"
    assert result.attempts == 3


@pytest.mark.asyncio
async def test_auth_error_fails_fast():
    """401 should not burn retries."""
    client = FlakyClient(fail_times=99, error="401 unauthorized")
    result = await call_with_recovery(client, "m", [{"role": "user", "content": "hi"}], None, base_delay=0.01)
    assert result.ok is False
    assert result.attempts == 1  # no retry on auth


@pytest.mark.asyncio
async def test_fallback_chain():
    """Primary model down -> falls back to next model in chain."""
    primary = FlakyClient(fail_times=99, error="503 service unavailable")
    backup = FlakyClient(fail_times=0)

    def resolver(model_id):
        return backup, model_id

    result = await call_with_recovery(
        primary, "m1", [{"role": "user", "content": "hi"}],
        resolver, fallback_chain=["m2-local"], base_delay=0.01,
    )
    assert result.ok is True
    assert result.fell_back_to == "m2-local"
    assert result.model_used == "m2-local"


@pytest.mark.asyncio
async def test_all_candidates_fail():
    bad1 = FlakyClient(99, "503 down")
    bad2 = FlakyClient(99, "timeout")
    result = await call_with_recovery(
        bad1, "m1", [{"role": "user", "content": "hi"}],
        lambda mid: (bad2, mid), fallback_chain=["m2"], base_delay=0.01,
    )
    assert result.ok is False
    assert len(result.errors) >= 2
