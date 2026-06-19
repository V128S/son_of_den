from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    business_bot_token: SecretStr
    panel_bot_analyst_token: SecretStr
    panel_bot_skeptic_token: SecretStr
    panel_bot_creative_token: SecretStr
    panel_bot_pragmatist_token: SecretStr
    panel_bot_moderator_token: SecretStr
    panel_chat_id: int
    admin_user_id: int
    claude_model: str = "claude-sonnet-4-6"

    # Optional Groq fallback — used when Claude rate-limits or its circuit breaker opens.
    # If groq_api_key is unset, the bot falls back to persona.fallback templates instead.
    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"

    # OpenRouter API for DeepSeek, Owl-Alpha, Gemini and Nemotron models
    openrouter_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek/deepseek-v4-flash:free"
    owl_alpha_model: str = "openrouter/owl-alpha"
    gemini_lite_model: str = "google/gemini-3.1-flash-lite"
    nemotron_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"

    # Google Gemini API (optional alternative for moderator)
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.0-flash"

    # Provider for the moderator: "claude" or "gemini"
    moderator_provider: str = "claude"

    # Google Calendar Integration settings
    google_service_account_file: Path | None = None
    google_calendar_id: str = "primary"
    user_timezone: str = "Europe/Moscow"

    log_level: str = "INFO"
    # Log file path. Empty string = stdout only (Docker/systemd mode).
    log_file: str = ""
    # Rotating log: max size per file (bytes) and number of backup files.
    log_max_bytes: int = 5 * 1024 * 1024   # 5 MB
    log_backup_count: int = 3
    personas_path: Path = Path("personas.yaml")

    # File that persists topic mappings across restarts (prevents duplicate forum topics).
    # Use an absolute path if the working directory might change.
    state_file: Path = Path("bot_state.json")

    # How often the panel bots spontaneously revive past discussions (hours).
    # Set to 0 to disable the revival scheduler entirely.
    panel_revival_interval_hours: float = 0.0

    # Time of day to send the daily contact digest (HH:MM in user_timezone).
    # Set to empty string to disable.
    contact_digest_time: str = "20:00"

    # Feed monitor: auto-topics from Telegram channels via RSS.
    # Comma-separated list of channel slugs, e.g. "durov,techcrunch".
    # Empty string disables the feed monitor entirely.
    feed_channels: str = ""
    feed_interests: str = "технологии, AI, бизнес, стартапы"
    feed_max_per_day: int = 2
    feed_check_interval_hours: float = 1.0
    feed_min_score: int = 7
    # Minimum hours between two auto-triggered rounds (set low for testing).
    feed_min_interval_hours: float = 4.0
    # Obsidian vault integration — local path to the vault folder.
    # Leave empty to disable Obsidian logging.
    obsidian_vault_path: str = ""

    # Google Sheets integration — personal price sheet where transferred rows land.
    sheets_personal_id: str = ""
    # Markup percentage applied to prices when transferring from contact's sheet to yours.
    sheets_markup_percent: float = 20.0

    # Utility meter readings — Google Sheet ID for electricity/water/gas tracker.
    # Leave empty to disable meter readings integration.
    meters_sheet_id: str = ""

    # Morning briefing — AI-generated daily summary sent to admin each morning.
    # Set to empty string to disable.
    morning_briefing_time: str = "09:00"

    # Exa web search — optional enrichment for panel discussions.
    # Leave empty to disable web search (panel works without it).
    exa_api_key: SecretStr | None = None

    # Daily news panel — triggers one panel discussion every morning at the given local time.
    # The topic is built from yesterday's top news matching daily_news_interests.
    # Set to empty string to disable.
    daily_news_panel_time: str = ""
    # Interests for the daily news search (falls back to feed_interests when empty).
    daily_news_interests: str = ""

    # Follow-up reminder: days of contact silence before the admin is reminded.
    # Set to 0 to disable. Checked every 12 hours.
    contact_followup_days: int = 0

    # Daily token cost alert threshold in USD. Set to 0.0 to disable.
    # When daily estimated cost exceeds this, the admin gets a one-time Telegram alert.
    daily_cost_alert_usd: float = 0.0

    # Daily feed digest — AI summary of all channel posts from the past 24 h.
    # Set to empty string to disable. Format: "HH:MM" in user_timezone.
    feed_digest_time: str = ""

    # Expense tracker — Google Sheet ID where parsed expenses are appended.
    # Leave empty to disable.
    expenses_sheet_id: str = ""

