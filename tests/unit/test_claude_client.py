from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import APIStatusError, RateLimitError

from claudebots.core.circuit_breaker import CircuitBreakerOpen
from claudebots.core.claude_client import ClaudeClient


def _make_response(
    text: str, input_tokens: int = 100, output_tokens: int = 20, cache_read: int = 0
):
    response = MagicMock()
    content_block = MagicMock(text=text, type="text")
    response.content = [content_block]
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.usage.cache_read_input_tokens = cache_read
    return response


@pytest.fixture
def mock_sdk():
    sdk = MagicMock()
    sdk.messages.create = AsyncMock(return_value=_make_response("hello"))
    return sdk


async def test_complete_returns_text(mock_sdk):
    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = mock_sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = MagicMock(check=MagicMock(return_value=None))
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    result = await client.complete(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert result == "hello"
    assert client.usage == {"input": 100, "output": 20, "cache_read": 0}


async def test_system_uses_cache_control(mock_sdk):
    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = mock_sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = MagicMock(check=MagicMock(return_value=None))
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    await client.complete(system="my system", messages=[{"role": "user", "content": "x"}])

    kwargs = mock_sdk.messages.create.call_args.kwargs
    assert kwargs["system"] == [
        {"type": "text", "text": "my system", "cache_control": {"type": "ephemeral"}}
    ]
    assert kwargs["model"] == "claude-sonnet-4-6"


async def test_breaker_open_raises_immediately(mock_sdk):
    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = mock_sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = MagicMock(check=MagicMock(side_effect=CircuitBreakerOpen()))
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    with pytest.raises(CircuitBreakerOpen):
        await client.complete(system="x", messages=[{"role": "user", "content": "x"}])

    mock_sdk.messages.create.assert_not_called()


async def test_failure_records_in_breaker():
    sdk = MagicMock()
    err = APIStatusError("boom", response=MagicMock(status_code=500), body=None)
    sdk.messages.create = AsyncMock(side_effect=err)
    breaker = MagicMock(check=MagicMock(return_value=None))

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    with pytest.raises(APIStatusError):
        await client.complete(system="x", messages=[{"role": "user", "content": "x"}])

    assert breaker.record_failure.call_count >= 1


async def test_success_records_success_in_breaker(mock_sdk):
    breaker = MagicMock(check=MagicMock(return_value=None))
    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = mock_sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    await client.complete(system="x", messages=[{"role": "user", "content": "x"}])

    breaker.record_success.assert_called_once()


# ── Streaming ──────────────────────────────────────────────────────────────


class _FakeStream:
    """Mocks the async-context-manager returned by `sdk.messages.stream(...)`."""

    def __init__(self, chunks: list[str], usage: dict[str, int] | None = None):
        self._chunks = chunks
        self._usage = usage or {"input": 100, "output": 20, "cache_read": 0}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk

    async def get_final_message(self):
        m = MagicMock()
        m.usage.input_tokens = self._usage["input"]
        m.usage.output_tokens = self._usage["output"]
        m.usage.cache_read_input_tokens = self._usage["cache_read"]
        return m


class _RaisingStream:
    """Mocks a stream context manager that raises on enter (simulates auth/network failure)."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


async def test_stream_yields_deltas_and_tracks_usage():
    sdk = MagicMock()
    sdk.messages.stream = MagicMock(
        return_value=_FakeStream(
            ["Hello", ", ", "world!"],
            usage={"input": 100, "output": 20, "cache_read": 5},
        )
    )
    breaker = MagicMock(check=MagicMock(return_value=None))

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    chunks = []
    async for delta in client.stream(system="sys", messages=[{"role": "user", "content": "hi"}]):
        chunks.append(delta)

    assert chunks == ["Hello", ", ", "world!"]
    assert client.usage == {"input": 100, "output": 20, "cache_read": 5}
    breaker.record_success.assert_called_once()


async def test_stream_applies_cache_control_to_system():
    sdk = MagicMock()
    sdk.messages.stream = MagicMock(return_value=_FakeStream(["x"]))
    breaker = MagicMock(check=MagicMock(return_value=None))

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    async for _ in client.stream(system="my sys", messages=[{"role": "user", "content": "x"}]):
        pass

    kwargs = sdk.messages.stream.call_args.kwargs
    assert kwargs["system"] == [
        {"type": "text", "text": "my sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert kwargs["model"] == "claude-sonnet-4-6"


async def test_stream_failure_records_breaker_and_propagates():
    err = APIStatusError("boom", response=MagicMock(status_code=500), body=None)
    sdk = MagicMock()
    sdk.messages.stream = MagicMock(return_value=_RaisingStream(err))
    breaker = MagicMock(check=MagicMock(return_value=None))

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    with pytest.raises(APIStatusError):
        async for _ in client.stream(system="x", messages=[{"role": "user", "content": "x"}]):
            pass

    assert breaker.record_failure.call_count >= 1
    breaker.record_success.assert_not_called()


async def test_stream_breaker_open_raises_immediately():
    sdk = MagicMock()
    sdk.messages.stream = MagicMock(return_value=_FakeStream(["x"]))
    breaker = MagicMock(check=MagicMock(side_effect=CircuitBreakerOpen()))

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    with pytest.raises(CircuitBreakerOpen):
        async for _ in client.stream(system="x", messages=[{"role": "user", "content": "x"}]):
            pass

    sdk.messages.stream.assert_not_called()


# ── Groq fallback ──────────────────────────────────────────────────────────


def _rate_limit_error() -> RateLimitError:
    return RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)


async def test_complete_falls_back_to_groq_on_rate_limit():
    sdk = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=_rate_limit_error())
    breaker = MagicMock(check=MagicMock(return_value=None))

    fallback = MagicMock()
    fallback.complete = AsyncMock(return_value="groq says hi")

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = fallback
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    result = await client.complete(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert result == "groq says hi"
    fallback.complete.assert_awaited_once_with("sys", [{"role": "user", "content": "hi"}], 1024)
    breaker.record_failure.assert_called_once()
    breaker.record_success.assert_not_called()


async def test_complete_falls_back_when_breaker_open():
    sdk = MagicMock()
    sdk.messages.create = AsyncMock(return_value=_make_response("should not be called"))
    breaker = MagicMock(check=MagicMock(side_effect=CircuitBreakerOpen()))

    fallback = MagicMock()
    fallback.complete = AsyncMock(return_value="groq says hi")

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = fallback
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    result = await client.complete(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert result == "groq says hi"
    fallback.complete.assert_awaited_once()
    # Claude SDK was NOT called when breaker was open.
    sdk.messages.create.assert_not_called()


async def test_complete_rate_limit_without_fallback_raises():
    sdk = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=_rate_limit_error())
    breaker = MagicMock(check=MagicMock(return_value=None))

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = None
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    with pytest.raises(RateLimitError):
        await client.complete(system="x", messages=[{"role": "user", "content": "x"}])

    breaker.record_failure.assert_called_once()


async def test_complete_does_not_fall_back_on_5xx():
    """Generic 500 errors should propagate, not trigger fallback (transient — retry)."""
    err = APIStatusError("boom", response=MagicMock(status_code=500), body=None)
    sdk = MagicMock()
    sdk.messages.create = AsyncMock(side_effect=err)
    breaker = MagicMock(check=MagicMock(return_value=None))

    fallback = MagicMock()
    fallback.complete = AsyncMock(return_value="should not be called")

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = fallback
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    with pytest.raises(APIStatusError):
        await client.complete(system="x", messages=[{"role": "user", "content": "x"}])

    fallback.complete.assert_not_called()


async def test_stream_falls_back_to_groq_when_breaker_open():
    sdk = MagicMock()
    sdk.messages.stream = MagicMock(return_value=_FakeStream(["should not be used"]))
    breaker = MagicMock(check=MagicMock(side_effect=CircuitBreakerOpen()))

    async def fallback_stream(system, messages, max_tokens):
        for chunk in ["from", " ", "groq"]:
            yield chunk

    fallback = MagicMock()
    fallback.stream = fallback_stream

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = fallback
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    chunks = []
    async for d in client.stream(system="x", messages=[{"role": "user", "content": "x"}]):
        chunks.append(d)

    assert chunks == ["from", " ", "groq"]
    sdk.messages.stream.assert_not_called()


async def test_stream_falls_back_to_groq_on_rate_limit_before_yielding():
    sdk = MagicMock()
    sdk.messages.stream = MagicMock(return_value=_RaisingStream(_rate_limit_error()))
    breaker = MagicMock(check=MagicMock(return_value=None))

    async def fallback_stream(system, messages, max_tokens):
        for chunk in ["g", "r", "oq"]:
            yield chunk

    fallback = MagicMock()
    fallback.stream = fallback_stream

    client = ClaudeClient.__new__(ClaudeClient)
    client._sdk = sdk
    client._model = "claude-sonnet-4-6"
    client._breaker = breaker
    client._fallback = fallback
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    chunks = []
    async for d in client.stream(system="x", messages=[{"role": "user", "content": "x"}]):
        chunks.append(d)

    assert chunks == ["g", "r", "oq"]
    breaker.record_failure.assert_called_once()
