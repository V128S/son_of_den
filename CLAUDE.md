# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the bot
uv run python -m claudebots

# Run all tests (68 tests, e2e excluded by default)
uv run pytest

# Run unit tests only (fast, no external deps)
uv run pytest tests/unit

# Run a single test file
uv run pytest tests/unit/test_business_router.py

# Run a single test by name
uv run pytest tests/unit/test_config.py::test_name

# Lint
uv run ruff check .
uv run ruff format .

# Type-check (strict mode, core only)
uv run mypy claudebots/core

# Local restart loop (dev)
bash run_bot.sh
```

## Architecture

One Python process, one asyncio event loop, 6 Telegram bots. All bots share a single `Dispatcher` with three routers registered in order: `panel_router` → `business_router` → `admin_router`. Router order matters — panel messages must be checked before business messages.

### Entry point and dependency injection (`__main__.py`)

`amain()` builds all dependencies, then injects them via `dp.workflow_data`. Every aiogram handler receives objects like `ai_registry`, `conv`, `personas`, `bots`, `alerts`, `calendar_client`, `settings` as keyword arguments — no globals in handler signatures.

Background tasks started at boot:
- **Session guardian** — calls `Bot.close()` on all bots every 40 s to evict any competing process
- **Revival scheduler** — spontaneously re-opens past panel discussions at a configurable interval (`PANEL_REVIVAL_INTERVAL_HOURS`)
- **Daily digest scheduler** — sends a contact summary to the admin at `CONTACT_DIGEST_TIME`
- **Feed monitor** — polls Telegram channel RSS and auto-triggers panel rounds for scored posts

### Core modules (`claudebots/core/`)

| Module | Purpose |
|---|---|
| `config.py` | `Settings` (pydantic-settings from `.env`). Single source of truth for all env vars. |
| `ai_registry.py` | `AIRegistry` — maps provider name strings (`"claude"`, `"groq"`, `"openrouter_deepseek"`, etc.) to `AIClient` instances. All clients satisfy the `AIClient` Protocol: `complete()` + `stream()` + `usage`. |
| `claude_client.py` | Wraps Anthropic SDK. Uses prompt caching (`cache_control: ephemeral` on system blocks). Has `CircuitBreaker` built in; falls back to Groq on open. |
| `circuit_breaker.py` | Sliding-window failure counter. `CLOSED → OPEN` after N failures in window; `OPEN → HALF_OPEN` after recovery period; `HALF_OPEN → CLOSED` on success. |
| `conversation.py` | `ConversationStore` — per-key ring buffer (deque with `maxlen=40`). Keys are namespaced strings like `"biz:{conn_id}:{chat_id}"` or `"private:{chat_id}:{thread_id}"`. |
| `personas.py` | Loads `personas.yaml` into `PersonaRegistry`. Supports `<<MARKER>>` template substitution across prompts. `/reload` hot-reloads without restart. |
| `state.py` | Atomic JSON persistence for topic mappings (`bot_state.json`). `load()` / `save()` / `update()`. Helpers for int-key encoding (JSON requires string keys). |
| `alerts.py` | `AlertSender` — sends throttled admin notifications (one per `alert_key` per window, 4096-char truncation). |
| `calendar_client.py` | Reads Google Calendar via Service Account. 60 s in-memory cache. Optional — bot works without it. |
| `feed_monitor.py` | Polls `rsshub.app/telegram/channel/<slug>` (Atom); falls back to `t.me/s/<slug>` scraping on 403. Scores entries with the cheapest available AI; fires `PanelRoundRunner` when score ≥ `FEED_MIN_SCORE`. |

### Routers (`claudebots/routers/`)

**`business.py`** handles two distinct message types on the same bot:
- **Business messages** (`@business_router.business_message`) — auto-replies on behalf of the owner using streaming. Mirrors incoming and outgoing messages to the admin via per-contact forum topics.
- **Private/supergroup messages** (`_on_private_message`) — owner-mode vs. regular-user mode. Owner gets a non-streaming `complete()` call; non-owner gets the streaming placeholder flow. Forwarded channel posts trigger a panel round via `_on_forward_to_panel`.
- **Owner topic categorisation** — when the admin writes in a forum topic (not a contact topic), the message is classified into one of 9 fixed categories and routed to the matching topic (or the topic is renamed). Uses `openrouter_gemini` for classification.
- Module-level dicts (`_contact_topics`, `_admin_topics`, etc.) are the in-memory state; `_persist_business_state()` flushes them to `bot_state.json` via `state.update()`.

**`panel.py`** orchestrates the 5-bot discussion:
- `PanelRoundRunner.run_round(topic)` — sequential round: each speaker responds in turn, then the moderator synthesises. Uses `_processing_lock` to ensure at most one active round at a time.
- Forum topics are created/reused per discussion category. `_panel_topics` (thread_id → name) and `_panel_memories` (up to 7 compact takeaways) are persisted in `bot_state.json`.
- Revival scheduler picks a random past memory and re-opens it informally.

**`admin.py`** — `/ping`, `/reset`, `/cost`, `/reload` commands. `PersonaHolder` wraps `PersonaRegistry` so `/reload` can swap the registry in-place.

### Persona system (`personas.yaml`)

Each persona specifies `provider`, `system_prompt`, `max_tokens`, `fallback`, and `bot_token_env`. The `provider` key must match a key registered in `AIRegistry`. Prompts support `<<MARKER>>` placeholders resolved from `base_prompts` / `shared` YAML sections. `common_system_prompt` in the panel section is injected as `<<COMMON_PANEL_PROMPT>>`.

### Multi-model routing

Each persona declares its own `provider`. `AIRegistry.get_client(provider)` returns the right client. `ClaudeClient` is the only client with a circuit breaker and Groq fallback. All other clients (`GroqClient`, `OpenRouterClient`, `GeminiClient`) expose the same `AIClient` Protocol and track `usage` (input/output/cache_read tokens).

### State persistence

`bot_state.json` stores these keys:
- `panel_topics` — `{thread_id: topic_name}` for the panel forum
- `contact_topics` — `{user_id: thread_id}` for business contacts
- `admin_topics` — `{category_name: thread_id}` for owner categories
- `feed_seen` — set of already-processed RSS entry IDs
- `feed_last_round_ts` / `feed_rounds_today` / `feed_date_today` — feed rate-limiting

### Testing

Tests use `pytest-asyncio` in auto mode (`asyncio_mode = "auto"` in `pyproject.toml`). `conftest.py` provides `conv`, `personas`, `ai_registry_mock`, `bot_mocks`, `alerts_mock` fixtures. AI clients are mocked with `AsyncMock`; Telegram bots are `MagicMock` with `AsyncMock` methods. E2E tests require real tokens and are excluded by default (`-m 'not e2e'`).

`mypy` runs in strict mode only on `claudebots/core`; routers use non-strict mode.

## Key conventions

- **Conversation keys**: `"biz:{business_connection_id}:{chat_id}"` for business messages, `"private:{chat_id}:{thread_id}"` for direct messages (thread_id=0 for main chat), `"panel:{chat_id}:{thread_id}"` for panel rounds.
- **Streaming pattern**: send `"…"` placeholder → edit at ≥ 1 s intervals as chunks arrive → final edit with full text. `parse_mode=None` throughout to avoid errors on partial HTML.
- **Fallback chain**: if `stream()` raises → use `persona.fallback` string.
- **No AI attribution in git commits** — do not add Co-Authored-By or similar footers.
