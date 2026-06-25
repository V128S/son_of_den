"""Unit tests for the OpenModel (deepseek-v4-flash) client.

OpenModel speaks the Anthropic Messages protocol, so the client drives the
`anthropic` SDK against a custom base_url.  The SDK is mocked here — we only
verify our own glue: message normalisation, text-block extraction, and usage
accounting.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from claudebots.core.openmodel_client import OpenModelClient, _normalize_messages

# ---------------------------------------------------------------------------
# _normalize_messages — Anthropic requires strict user/assistant alternation
# ---------------------------------------------------------------------------

def test_normalize_merges_consecutive_assistant_turns():
    # The panel appends one assistant message per speaker — they must collapse.
    msgs = [
        {"role": "user", "content": "тема"},
        {"role": "assistant", "content": "[Аналитик]: раз"},
        {"role": "assistant", "content": "[Скептик]: два"},
        {"role": "user", "content": "итог"},
    ]
    out = _normalize_messages(msgs)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "user"]
    assert "раз" in out[1]["content"] and "два" in out[1]["content"]


def test_normalize_drops_leading_assistant():
    msgs = [
        {"role": "assistant", "content": "осиротевший"},
        {"role": "user", "content": "вопрос"},
    ]
    out = _normalize_messages(msgs)
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "вопрос"


def test_normalize_never_returns_empty():
    assert _normalize_messages([]) == [{"role": "user", "content": "."}]


# ---------------------------------------------------------------------------
# complete() — extracts only text blocks, records usage
# ---------------------------------------------------------------------------

async def test_complete_extracts_text_blocks_only():
    client = OpenModelClient(api_key="om-test", model="deepseek-v4-flash")

    # deepseek thinking mode emits a `thinking` block before the `text` block —
    # only the text must survive.
    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="скрытые рассуждения"),
            SimpleNamespace(type="text", text="Видимый ответ."),
        ],
        usage=SimpleNamespace(input_tokens=12, output_tokens=5, cache_read_input_tokens=0),
    )
    client._sdk.messages.create = AsyncMock(return_value=fake_response)

    out = await client.complete(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert out == "Видимый ответ."
    assert client.usage["input"] == 12
    assert client.usage["output"] == 5


async def test_complete_passes_normalized_messages():
    client = OpenModelClient(api_key="om-test")
    create = AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
    ))
    client._sdk.messages.create = create

    await client.complete(
        system="sys",
        messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "assistant", "content": "c"},
        ],
    )
    sent = create.call_args.kwargs["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant"]
