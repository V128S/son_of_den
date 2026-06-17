"""Regression: /help must reach its own handler, not be swallowed by the
catch-all owner message handler.

In aiogram the first matching handler in a router wins. The catch-all
``_on_private_message`` matches any text in private/supergroup, so the
``_on_help`` handler must be registered *before* it — otherwise /help is
treated as a normal owner message and the help text is never sent.
"""
from claudebots.routers import business


def _callbacks():
    return [h.callback for h in business.business_router.message.handlers]


def test_help_handler_registered_before_catchall():
    cbs = _callbacks()
    assert business._on_help in cbs
    assert business._on_private_message in cbs
    assert cbs.index(business._on_help) < cbs.index(business._on_private_message), (
        "_on_help must be registered before the catch-all _on_private_message"
    )


def test_is_help_cmd_matches():
    assert business._is_help_cmd("/help")
    assert business._is_help_cmd("help")
    assert business._is_help_cmd("/help подробнее")
    assert not business._is_help_cmd("/helpme")
    assert not business._is_help_cmd("hello")
    assert not business._is_help_cmd(None)


def test_router_order_admin_before_business():
    """admin's Command() handlers must run before the business catch-all,
    otherwise /cost, /stats, /panelschedule … are swallowed by _on_private_message."""
    from claudebots.__main__ import ROUTER_ORDER
    from claudebots.routers.admin import admin_router
    from claudebots.routers.panel import panel_router

    assert ROUTER_ORDER[0] is panel_router  # panel still first
    assert ROUTER_ORDER.index(admin_router) < ROUTER_ORDER.index(business.business_router)


async def test_business_catchall_would_swallow_a_slash_command():
    """Documents WHY the order matters: the business catch-all matches a bare
    slash command, so any command-router must be checked before it."""

    class _Msg:
        text = "/stats"
        business_connection_id = None
        forward_from_chat = None
        forward_origin = None

        class chat:
            type = "private"
            id = 1

        class from_user:
            id = 42
            is_bot = False

    handler = next(
        h for h in business.business_router.message.handlers
        if h.callback is business._on_private_message
    )
    matched, _ = await handler.check(_Msg())
    assert matched is True
