import asyncio
import logging
import sys

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

from claudebots.bots import create_all_bots
from claudebots.core.ai_registry import AIClient, AIRegistry
from claudebots.core.alerts import AlertSender
from claudebots.core.claude_client import ClaudeClient
from claudebots.core.config import Settings
from claudebots.core.conversation import ConversationStore
from claudebots.core.gemini_client import GeminiClient
from claudebots.core.groq_client import GroqClient
from claudebots.core.calendar_client import GoogleCalendarClient
from claudebots.core.openrouter_client import OpenRouterClient
from claudebots.core.personas import load_personas
from claudebots.routers.admin import PersonaHolder, admin_router
from claudebots.routers.business import business_router
from claudebots.routers.panel import panel_router

logger = logging.getLogger(__name__)


async def amain() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    registry = load_personas(settings.personas_path)
    persona_holder = PersonaHolder(registry=registry)

    # Initialize AI clients for multi-model architecture
    clients: dict[str, AIClient] = {}

    # Groq client (for analyst, skeptic)
    if settings.groq_api_key is not None:
        groq = GroqClient(
            api_key=settings.groq_api_key.get_secret_value(),
            model=settings.groq_model,
        )
        clients["groq"] = groq
        logger.info("Groq client enabled (model=%s)", settings.groq_model)
    else:
        logger.warning("Groq client disabled (GROQ_API_KEY not set)")

    # OpenRouter clients (for creative, pragmatist, business, moderator)
    if settings.openrouter_api_key is not None:
        openrouter_key = settings.openrouter_api_key.get_secret_value()
        clients["openrouter_deepseek"] = OpenRouterClient(
            api_key=openrouter_key,
            model=settings.deepseek_model,
        )
        clients["openrouter_owl"] = OpenRouterClient(
            api_key=openrouter_key,
            model=settings.owl_alpha_model,
        )
        clients["openrouter_gemini"] = OpenRouterClient(
            api_key=openrouter_key,
            model=settings.gemini_lite_model,
        )
        logger.info(
            "OpenRouter clients enabled (deepseek=%s, owl=%s, gemini=%s)",
            settings.deepseek_model,
            settings.owl_alpha_model,
            settings.gemini_lite_model,
        )
    else:
        logger.warning("OpenRouter clients disabled (OPENROUTER_API_KEY not set)")

    # Gemini client (optional, for moderator)
    if settings.gemini_api_key is not None:
        clients["gemini"] = GeminiClient(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
        )
        logger.info("Gemini client enabled (model=%s)", settings.gemini_model)
    else:
        logger.info("Gemini client disabled (GEMINI_API_KEY not set)")

    # Claude client (optional, with Groq fallback if available)
    if settings.anthropic_api_key is not None:
        fallback = clients.get("groq")  # type: ignore[assignment]
        claude = ClaudeClient(settings, fallback=fallback)
        clients["claude"] = claude
        logger.info("Claude client enabled (model=%s)", settings.claude_model)
    else:
        logger.info("Claude client disabled (ANTHROPIC_API_KEY not set)")

    # Create AI registry
    ai_registry = AIRegistry(clients)

    # Validate that all required providers are available
    required_providers: set[str] = set()
    required_providers.add(registry.business_assistant.provider)
    for p in registry.all_panel():
        required_providers.add(p.provider)

    missing = required_providers - set(clients.keys())
    if missing:
        raise RuntimeError(
            f"Missing API keys for providers: {missing}. "
            f"Check your .env file for required keys."
        )

    conv = ConversationStore()
    bots = create_all_bots(settings)
    alerts = AlertSender(bot=bots["business"], admin_user_id=settings.admin_user_id)

    calendar_client = GoogleCalendarClient(
        service_account_file=settings.google_service_account_file,
        calendar_id=settings.google_calendar_id,
        timezone_str=settings.user_timezone,
    )

    dp = Dispatcher()

    # Dependency injection via workflow_data — every handler receives these as kwargs
    dp.workflow_data.update(
        settings=settings,
        personas=persona_holder.registry,  # snapshot; /reload updates via holder below
        persona_holder=persona_holder,
        claude=claude,
        ai_registry=ai_registry,
        conv=conv,
        bots=bots,
        alerts=alerts,
        calendar_client=calendar_client,
    )

    dp.include_routers(business_router, panel_router, admin_router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> bool:
        logger.exception("Unhandled error", exc_info=event.exception)
        try:
            await alerts.send(
                f"unhandled_{type(event.exception).__name__}",
                str(event.exception)[:500],
            )
        except Exception:
            pass
        return True

    async def on_shutdown() -> None:
        logger.info("Shutting down: closing sessions")
        for b in bots.values():
            try:
                await b.session.close()
            except Exception:
                pass

    dp.shutdown.register(on_shutdown)

    logger.info("Starting polling on %d bots", len(bots))
    await dp.start_polling(*bots.values())


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
