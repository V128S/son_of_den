"""Integration tests for the admin forward-to-panel trigger in business_router."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message, User

from claudebots.routers.business import _on_forward_to_panel


def _make_message(*, from_user_id=42, chat_type="private", forward_chat_title="TechChannel",
                  text="Interesting news", caption=None, forward_from_chat=True):
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = from_user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.type = chat_type
    msg.text = text
    msg.caption = caption
    if forward_from_chat:
        msg.forward_from_chat = MagicMock()
        msg.forward_from_chat.title = forward_chat_title
    else:
        msg.forward_from_chat = None
    # forward_origin for Bot API 7.0+ (type annotation needs explicit set on spec=Message mock)
    msg.forward_origin = None
    msg.reply = AsyncMock()
    return msg


def _make_deps(*, admin_user_id=42):
    settings = MagicMock()
    settings.admin_user_id = admin_user_id
    settings.panel_chat_id = -100999

    ai_registry = MagicMock()
    conv = MagicMock()
    personas = MagicMock()
    alerts = MagicMock()
    bots = MagicMock()

    return dict(settings=settings, ai_registry=ai_registry, conv=conv,
                personas=personas, alerts=alerts, bots=bots)


@pytest.mark.asyncio
async def test_admin_forward_triggers_round():
    """Admin forwarding a message starts a PanelRoundRunner task."""
    msg = _make_message()
    deps = _make_deps()

    with patch("claudebots.routers.business.asyncio.create_task") as mock_create:
        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()
        mock_create.return_value = mock_task

        with patch("claudebots.routers.panel.PanelRoundRunner") as MockRunner:
            mock_runner = MagicMock()
            mock_runner.run_round = AsyncMock()
            MockRunner.return_value = mock_runner
            await _on_forward_to_panel(msg, **deps)

        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_admin_gets_confirmation_reply():
    """Admin receives a confirmation reply when the round is launched."""
    msg = _make_message()
    deps = _make_deps()

    with patch("claudebots.routers.business.asyncio.create_task") as mock_create:
        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()
        mock_create.return_value = mock_task
        with patch("claudebots.routers.panel.PanelRoundRunner"):
            await _on_forward_to_panel(msg, **deps)

    msg.reply.assert_called_once()
    reply_text = msg.reply.call_args[0][0]
    assert "панел" in reply_text.lower() or "обсужден" in reply_text.lower() or "запуск" in reply_text.lower()


@pytest.mark.asyncio
async def test_non_admin_ignored():
    """Forwarded message from non-admin is silently ignored."""
    msg = _make_message(from_user_id=9999)
    deps = _make_deps(admin_user_id=42)

    with patch("claudebots.routers.business.asyncio.create_task") as mock_create:
        await _on_forward_to_panel(msg, **deps)
        mock_create.assert_not_called()

    msg.reply.assert_not_called()


@pytest.mark.asyncio
async def test_no_text_sends_warning():
    """Forward with no text or caption replies with a warning, no round started."""
    msg = _make_message(text=None, caption=None)
    deps = _make_deps()

    with patch("claudebots.routers.business.asyncio.create_task") as mock_create:
        await _on_forward_to_panel(msg, **deps)
        mock_create.assert_not_called()

    msg.reply.assert_called_once()
    reply_text = msg.reply.call_args[0][0]
    assert "⚠️" in reply_text


@pytest.mark.asyncio
async def test_caption_used_when_no_text():
    """When text is None but caption is set, caption is used as topic source."""
    msg = _make_message(text=None, caption="Caption content here")
    deps = _make_deps()

    with patch("claudebots.routers.business.asyncio.create_task") as mock_create:
        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()
        mock_create.return_value = mock_task
        with patch("claudebots.routers.panel.PanelRoundRunner"):
            await _on_forward_to_panel(msg, **deps)

    mock_create.assert_called_once()
