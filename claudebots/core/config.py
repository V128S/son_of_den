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

    # OpenRouter API for DeepSeek, Owl-Alpha and Gemini models
    openrouter_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek/deepseek-v4-flash:free"
    owl_alpha_model: str = "openrouter/owl-alpha"
    gemini_lite_model: str = "google/gemini-3.1-flash-lite"

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
    personas_path: Path = Path("personas.yaml")

    # File that persists topic mappings across restarts (prevents duplicate forum topics).
    # Use an absolute path if the working directory might change.
    state_file: Path = Path("bot_state.json")

    # How often the panel bots spontaneously revive past discussions (hours).
    # Set to 0 to disable the revival scheduler entirely.
    panel_revival_interval_hours: float = 2.0

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

