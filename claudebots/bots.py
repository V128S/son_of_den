from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from claudebots.core.config import Settings


def create_all_bots(settings: Settings) -> dict[str, Bot]:
    session = AiohttpSession()
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)

    def make(token: str) -> Bot:
        return Bot(token=token, session=session, default=default)

    return {
        "business": make(settings.business_bot_token.get_secret_value()),
        "analyst": make(settings.panel_bot_analyst_token.get_secret_value()),
        "skeptic": make(settings.panel_bot_skeptic_token.get_secret_value()),
        "creative": make(settings.panel_bot_creative_token.get_secret_value()),
        "pragmatist": make(settings.panel_bot_pragmatist_token.get_secret_value()),
        "moderator": make(settings.panel_bot_moderator_token.get_secret_value()),
    }
