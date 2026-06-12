from unittest.mock import AsyncMock, MagicMock

from claudebots.core.groq_client import GroqClient


def _make_groq_response(text: str, prompt_tokens: int = 50, completion_tokens: int = 10):
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=text))]
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    return response


async def test_complete_returns_text_and_tracks_usage():
    sdk = MagicMock()
    sdk.chat.completions.create = AsyncMock(
        return_value=_make_groq_response("hello from groq", prompt_tokens=42, completion_tokens=7)
    )

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client._model = "llama-3.3-70b-versatile"
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    result = await client.complete(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert result == "hello from groq"
    assert client.usage == {"input": 42, "output": 7, "cache_read": 0}


async def test_complete_passes_system_as_first_message():
    sdk = MagicMock()
    sdk.chat.completions.create = AsyncMock(return_value=_make_groq_response("ok"))

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client._model = "llama-3.3-70b-versatile"
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    await client.complete(
        system="you are helpful",
        messages=[{"role": "user", "content": "hello"}],
    )

    kwargs = sdk.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "llama-3.3-70b-versatile"
    assert kwargs["messages"] == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hello"},
    ]


async def test_complete_handles_null_content():
    """Groq may return None for content; we should return an empty string."""
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=None))]
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 0

    sdk = MagicMock()
    sdk.chat.completions.create = AsyncMock(return_value=response)

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client._model = "llama-3.3-70b-versatile"
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    result = await client.complete(system="x", messages=[{"role": "user", "content": "x"}])

    assert result == ""


# ── Streaming ──────────────────────────────────────────────────────────────


class _GroqStream:
    """Async iterator yielding OpenAI-shaped chunks for streaming Groq responses."""

    def __init__(self, chunks: list[str], usage: dict[str, int] | None = None):
        self._chunks = chunks
        self._usage = usage

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for c in self._chunks:
            chunk = MagicMock()
            chunk.choices = [MagicMock(delta=MagicMock(content=c))]
            chunk.usage = None
            yield chunk
        if self._usage is not None:
            final = MagicMock()
            final.choices = []
            final.usage.prompt_tokens = self._usage["input"]
            final.usage.completion_tokens = self._usage["output"]
            yield final


async def test_stream_yields_deltas_and_tracks_usage():
    sdk = MagicMock()
    sdk.chat.completions.create = AsyncMock(
        return_value=_GroqStream(["Hel", "lo", "!"], usage={"input": 33, "output": 3})
    )

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client._model = "llama-3.3-70b-versatile"
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    deltas = []
    async for d in client.stream(system="sys", messages=[{"role": "user", "content": "x"}]):
        deltas.append(d)

    assert deltas == ["Hel", "lo", "!"]
    assert client.usage == {"input": 33, "output": 3, "cache_read": 0}


async def test_stream_skips_empty_deltas():
    """Some chunks may have delta.content=None — these should not be yielded."""
    sdk = MagicMock()

    class _MixedStream:
        def __aiter__(self):
            return self._iter()

        async def _iter(self):
            for content in ["A", None, "B"]:
                chunk = MagicMock()
                chunk.choices = [MagicMock(delta=MagicMock(content=content))]
                chunk.usage = None
                yield chunk

    sdk.chat.completions.create = AsyncMock(return_value=_MixedStream())

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client._model = "llama-3.3-70b-versatile"
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    deltas = []
    async for d in client.stream(system="x", messages=[{"role": "user", "content": "x"}]):
        deltas.append(d)

    assert deltas == ["A", "B"]


async def test_stream_passes_stream_options():
    sdk = MagicMock()
    sdk.chat.completions.create = AsyncMock(return_value=_GroqStream(["x"]))

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client._model = "llama-3.3-70b-versatile"
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    async for _ in client.stream(system="s", messages=[{"role": "user", "content": "x"}]):
        pass

    kwargs = sdk.chat.completions.create.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}


# ── Voice transcription ────────────────────────────────────────────────────


async def test_transcribe_voice_returns_text():
    transcription = MagicMock()
    transcription.__str__ = lambda self: "Привет мир"

    sdk = MagicMock()
    sdk.audio.transcriptions.create = AsyncMock(return_value="Привет мир")

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    result = await client.transcribe_voice(b"fake-ogg-bytes", filename="voice.ogg", language="ru")

    assert result == "Привет мир"
    kwargs = sdk.audio.transcriptions.create.call_args.kwargs
    assert kwargs["model"] == "whisper-large-v3-turbo"
    assert kwargs["language"] == "ru"
    assert kwargs["response_format"] == "text"


async def test_transcribe_voice_strips_whitespace():
    sdk = MagicMock()
    sdk.audio.transcriptions.create = AsyncMock(return_value="  hello world  \n")

    client = GroqClient.__new__(GroqClient)
    client._sdk = sdk
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    result = await client.transcribe_voice(b"bytes")
    assert result == "hello world"
