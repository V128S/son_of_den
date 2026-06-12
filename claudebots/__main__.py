import asyncio
import logging
import sys

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

from claudebots.bots import create_all_bots
from claudebots.core import state as _state
from claudebots.core.ai_registry import AIClient, AIRegistry
from claudebots.core.alerts import AlertSender
from claudebots.core.calendar_client import GoogleCalendarClient
from claudebots.core.claude_client import ClaudeClient
from claudebots.core.config import Settings
from claudebots.core.conversation import ConversationStore
from claudebots.core.feed_monitor import start_feed_monitor
from claudebots.core.gemini_client import GeminiClient
from claudebots.core.groq_client import GroqClient
from claudebots.core.meters_client import MetersClient
from claudebots.core.obsidian_client import ObsidianClient
from claudebots.core.openrouter_client import OpenRouterClient
from claudebots.core.personas import load_personas
from claudebots.core.sheets_client import GoogleSheetsClient
from claudebots.routers.admin import PersonaHolder, admin_router
from claudebots.routers.briefing import start_briefing_scheduler
from claudebots.routers.business import business_router, init_business_state, start_digest_scheduler
from claudebots.routers.panel import (
    init_panel_state,
    panel_router,
    start_reminder_checker,
    start_revival_scheduler,
)
from claudebots.services.insta_downloader import InstagramDownloader
from claudebots.services.social_downloader import SocialDownloader
from claudebots.services.yt_downloader import YTDownloader

logger = logging.getLogger(__name__)


def _seize_sessions_sync(bots: dict) -> None:
    """Call Telegram close() synchronously to evict any competing bot process."""
    import urllib.request
    for b in bots.values():
        try:
            tok = b.token
            url = f"https://api.telegram.org/bot{tok}/close"
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=6):
                pass
        except Exception as exc:
            logger.debug("close() for bot %s: %s", tok[:10] + "...", exc)


async def _session_guardian(bots: dict) -> None:
    """Background task: evict competing instances every 40 s."""
    while True:
        await asyncio.sleep(40)
        logger.debug("Session guardian: evicting competing instances")
        await asyncio.to_thread(_seize_sessions_sync, bots)


async def _daily_usage_resetter(ai_registry) -> None:
    """Reset daily usage counters every UTC midnight."""
    from datetime import datetime, timezone, timedelta
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        delay = (next_midnight - now).total_seconds()
        await asyncio.sleep(delay)
        ai_registry.reset_daily_usage()


async def amain() -> None:
    settings = Settings()
    _log_fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    _handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        from logging.handlers import RotatingFileHandler
        _handlers.append(
            RotatingFileHandler(
                settings.log_file,
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
        )
    logging.basicConfig(level=settings.log_level, format=_log_fmt, handlers=_handlers)

    registry = load_personas(settings.personas_path)
    persona_holder = PersonaHolder(registry=registry)

    # Load persisted state (topic mappings) so we don't create duplicate forum topics
    # after a restart.  Done early so routers know existing thread IDs.
    _persisted = _state.load(settings.state_file)
    init_panel_state(settings.state_file, _persisted)
    init_business_state(settings.state_file, _persisted)

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
    claude: ClaudeClient | None = None
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

    # Restore persisted conversation history and usage counters
    if "conversations" in _persisted:
        conv.restore(_persisted["conversations"])
        logger.info("Conversation history restored (%d keys)", len(_persisted["conversations"]))
    if "usage" in _persisted:
        ai_registry.restore_usage(_persisted["usage"])
        logger.info("Usage counters restored from previous session")

    bots = create_all_bots(settings)
    alerts = AlertSender(bot=bots["business"], admin_user_id=settings.admin_user_id)

    calendar_client = GoogleCalendarClient(
        service_account_file=settings.google_service_account_file,
        calendar_id=settings.google_calendar_id,
        timezone_str=settings.user_timezone,
    )

    # Obsidian vault client (disabled when OBSIDIAN_VAULT_PATH is empty)
    obsidian_client: ObsidianClient | None = None
    if settings.obsidian_vault_path:
        obsidian_client = ObsidianClient(
            vault_path=settings.obsidian_vault_path,
            timezone_str=settings.user_timezone,
        )
        logger.info("Obsidian client enabled (vault=%s)", settings.obsidian_vault_path)
    else:
        logger.info("Obsidian client disabled (OBSIDIAN_VAULT_PATH not set)")

    # Google Sheets client (enabled when service account + personal sheet ID configured)
    sheets_client: GoogleSheetsClient | None = None
    if settings.google_service_account_file and settings.sheets_personal_id:
        sheets_client = GoogleSheetsClient(
            service_account_file=settings.google_service_account_file,
            personal_sheet_id=settings.sheets_personal_id,
            markup_percent=settings.sheets_markup_percent,
        )
        logger.info(
            "Google Sheets client enabled (personal_id=%s, markup=%.0f%%)",
            settings.sheets_personal_id[:8] + "..." if settings.sheets_personal_id else "",
            settings.sheets_markup_percent,
        )
    else:
        logger.info("Google Sheets client disabled (GOOGLE_SERVICE_ACCOUNT_FILE or SHEETS_PERSONAL_ID not set)")

    # Meter readings client (enabled when service account + meter sheet ID configured)
    meters_client: MetersClient | None = None
    if settings.google_service_account_file and settings.meters_sheet_id:
        meters_client = MetersClient(
            service_account_file=settings.google_service_account_file,
            sheet_id=settings.meters_sheet_id,
            timezone_str=settings.user_timezone,
        )
        logger.info("Meters client enabled (sheet=%s...)", settings.meters_sheet_id[:8])
    else:
        logger.info("Meters client disabled (GOOGLE_SERVICE_ACCOUNT_FILE or METERS_SHEET_ID not set)")

    # Instagram downloader (always available — uses yt-dlp, no credentials needed for public posts)
    insta_downloader = InstagramDownloader(timeout=90.0)
    logger.info("Instagram downloader enabled (yt-dlp, public posts and Reels only)")

    # YouTube audio downloader (always available — uses yt-dlp, no credentials needed for public videos)
    yt_downloader = YTDownloader(timeout=120.0)
    logger.info("YouTube audio downloader enabled (yt-dlp, public videos only)")

    # TikTok / X/Twitter downloader (always available — uses yt-dlp, public posts only)
    social_downloader = SocialDownloader(timeout=90.0)
    logger.info("Social downloader enabled (TikTok, X/Twitter via yt-dlp)")

    dp = Dispatcher()

    # Dependency injection via workflow_data — every handler receives these as kwargs
    workflow: dict = dict(
        settings=settings,
        personas=persona_holder.registry,  # snapshot; /reload updates via holder below
        persona_holder=persona_holder,
        ai_registry=ai_registry,
        conv=conv,
        bots=bots,
        alerts=alerts,
        calendar_client=calendar_client,
        obsidian_client=obsidian_client,
        sheets_client=sheets_client,
        meters_client=meters_client,
        insta_downloader=insta_downloader,
        yt_downloader=yt_downloader,
        social_downloader=social_downloader,
    )
    if claude is not None:
        workflow["claude"] = claude
    dp.workflow_data.update(workflow)

    # Panel router must be first to handle panel messages before business router
    dp.include_routers(panel_router, business_router, admin_router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> bool:
        import traceback as _tb
        logger.exception("Unhandled error", exc_info=event.exception)
        try:
            tb = "".join(_tb.format_exception(type(event.exception), event.exception,
                                              event.exception.__traceback__))
            await alerts.send(
                f"unhandled_{type(event.exception).__name__}",
                tb[-3500:],  # keep tail where the actual error is
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
        # Close AI client sessions (e.g. OpenRouter httpx)
        for name in ai_registry.providers:
            client = ai_registry.get_client(name)
            if hasattr(client, "close"):
                try:
                    await client.close()
                except Exception:
                    pass
        # Persist conversation history and usage counters
        try:
            _state.update(
                settings.state_file,
                {
                    "conversations": conv.snapshot(),
                    "usage": ai_registry.snapshot_usage(),
                },
            )
            logger.info("Conversation history and usage counters saved to %s", settings.state_file)
        except Exception as exc:
            logger.warning("Failed to persist conversations/usage on shutdown: %s", exc)

    dp.shutdown.register(on_shutdown)

    logger.info("Starting polling on %d bots", len(bots))

    # Start revival scheduler (disabled when interval == 0)
    revival_task: asyncio.Task[None] | None = None
    if settings.panel_revival_interval_hours > 0:
        revival_task = start_revival_scheduler(
            bots=bots,
            personas=persona_holder.registry,
            ai_registry=ai_registry,
            conv=conv,
            alerts=alerts,
            panel_chat_id=settings.panel_chat_id,
            interval_seconds=int(settings.panel_revival_interval_hours * 3600),
        )

    # Evict competing server instances at startup
    logger.info("Evicting competing bot sessions...")
    _seize_sessions_sync(bots)
    await asyncio.sleep(0.5)

    # Start background guardian to maintain session dominance
    guardian_task = asyncio.create_task(_session_guardian(bots))

    # Start daily usage reset task (resets at UTC midnight)
    ai_registry.reset_daily_usage()  # initialise baseline for the current boot
    daily_reset_task = asyncio.create_task(_daily_usage_resetter(ai_registry))

    # Start daily contact digest scheduler
    digest_task: asyncio.Task[None] | None = None
    if settings.contact_digest_time:
        digest_task = start_digest_scheduler(
            bot=bots["business"],
            admin_user_id=settings.admin_user_id,
            timezone_str=settings.user_timezone,
            digest_time=settings.contact_digest_time,
        )

    # Start reminder checker (re-surfaces action items after 18-20 h)
    reminder_task: asyncio.Task[None] = start_reminder_checker(
        bots=bots,
        panel_chat_id=settings.panel_chat_id,
    )

    # Start morning briefing scheduler
    briefing_task: asyncio.Task[None] | None = None
    if settings.morning_briefing_time:
        briefing_task = start_briefing_scheduler(
            bot=bots["business"],
            admin_user_id=settings.admin_user_id,
            timezone_str=settings.user_timezone,
            briefing_time=settings.morning_briefing_time,
            calendar_client=calendar_client,
            ai_registry=ai_registry,
        )
    else:
        logger.info("Morning briefing disabled (MORNING_BRIEFING_TIME not set)")

    # Start feed monitor (auto-topics from Telegram channel RSS)
    feed_task: asyncio.Task[None] | None = None
    feed_channels = [c.strip() for c in settings.feed_channels.split(",") if c.strip()]
    if feed_channels:
        feed_task = start_feed_monitor(
            channels=feed_channels,
            interests=settings.feed_interests,
            max_per_day=settings.feed_max_per_day,
            min_score=settings.feed_min_score,
            check_interval_seconds=int(settings.feed_check_interval_hours * 3600),
            min_interval_seconds=int(settings.feed_min_interval_hours * 3600),
            state_path=settings.state_file,
            ai_registry=ai_registry,
            bots=bots,
            personas=persona_holder.registry,
            conv=conv,
            alerts=alerts,
            panel_chat_id=settings.panel_chat_id,
        )
    else:
        logger.info("Feed monitor disabled (FEED_CHANNELS not set)")

    try:
        await dp.start_polling(*bots.values())
    finally:
        guardian_task.cancel()
        try:
            await guardian_task
        except asyncio.CancelledError:
            pass
        daily_reset_task.cancel()
        try:
            await daily_reset_task
        except asyncio.CancelledError:
            pass
        if revival_task is not None:
            revival_task.cancel()
            try:
                await revival_task
            except asyncio.CancelledError:
                pass
        if digest_task is not None:
            digest_task.cancel()
            try:
                await digest_task
            except asyncio.CancelledError:
                pass
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        if briefing_task is not None:
            briefing_task.cancel()
            try:
                await briefing_task
            except asyncio.CancelledError:
                pass
        if feed_task is not None:
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
