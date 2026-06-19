"""Unit tests for OpenRouterClient helpers."""


from claudebots.core.openrouter_client import _strip_thinking


def test_strip_removes_think_block():
    assert _strip_thinking("<think>internal reasoning</think>Final answer") == "Final answer"


def test_strip_multiline_think_block():
    raw = "<think>\nStep 1\nStep 2\n</think>\n\nActual response"
    assert _strip_thinking(raw) == "Actual response"


def test_strip_no_think_block_unchanged():
    assert _strip_thinking("No thinking here") == "No thinking here"


def test_strip_empty_string():
    assert _strip_thinking("") == ""


def test_strip_only_think_block_returns_empty():
    assert _strip_thinking("<think>just reasoning</think>") == ""


def test_strip_case_insensitive():
    assert _strip_thinking("<THINK>reasoning</THINK>answer") == "answer"


def test_strip_trims_surrounding_whitespace():
    assert _strip_thinking("<think>x</think>  \n  hello  \n") == "hello"


def test_strip_multiple_think_blocks():
    raw = "<think>first</think> middle <think>second</think> end"
    assert _strip_thinking(raw) == "middle  end"


def test_strip_preserves_content_after_think():
    raw = "<think>R</think>ВЫВОД: Ключевой вывод\nДЕЙСТВИЕ: Сделать X"
    result = _strip_thinking(raw)
    assert "ВЫВОД:" in result
    assert "<think>" not in result
