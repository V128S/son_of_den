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
