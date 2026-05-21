<div align="center">

# 🤖 Son of Den — Telegram Automation Suite

**Personal assistant + multi-bot discussion system for Telegram**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?style=flat&logo=telegram)](https://core.telegram.org/bots)
[![Tests](https://img.shields.io/badge/Tests-68%20passing-brightgreen?style=flat)](#testing)
[![License](https://img.shields.io/badge/License-Private-red?style=flat)](#)

</div>

---

## ✨ What it does

**Business Assistant** — auto-responds to Telegram messages on behalf of the owner. Understands context, reads Google Calendar in real time, and handles any question politely while the owner is busy. Incoming messages are mirrored to the admin in a dedicated forum topic per contact.

**Personal Assistant** — the owner can write directly to the business bot in any forum topic. Messages are automatically classified into thematic categories (Tasks, Ideas, Planning, Clients, etc.) and routed to the matching topic — or a new one is created and named accordingly.

**Panel Discussion** — 5 bots that debate any topic: Analyst, Skeptic, Creative, Pragmatist, and a Moderator who synthesises everything into a clean takeaway. Questions are automatically categorised and routed to thematic forum threads.

---

## 🏗 Architecture

```
One Python process · One asyncio loop · 6 Telegram bots

┌─────────────────┐    ┌──────────────────────────────────┐
│  Business Bot   │    │          Panel Bots               │
│  (auto-reply)   │    │  Analyst · Skeptic · Creative     │
│  + personal DM  │    │  Pragmatist · Moderator           │
└────────┬────────┘    └──────────────┬───────────────────┘
         │                            │
         └────────────┬───────────────┘
                      │
              ┌───────▼────────┐
              │   Dispatcher   │
              │  (aiogram 3.x) │
              └───────┬────────┘
                      │
         ┌────────────┼────────────┐
         │            │            │
   ┌─────▼──┐  ┌──────▼───┐ ┌────▼──────┐
   │Business│  │  Panel   │ │  Admin    │
   │ Router │  │  Router  │ │  Router   │
   └────────┘  └──────────┘ └───────────┘
```

**Core modules:**
- `ConversationStore` — per-chat message history with ring buffer
- `CircuitBreaker` — auto-fallback after consecutive API failures
- `GoogleCalendarClient` — live schedule fetching with 60 s cache
- `PersonaRegistry` — hot-reloadable YAML persona definitions
- `AlertSender` — throttled admin notifications (4096-char truncation)
- `AIRegistry` — multi-model routing (Claude · Groq · OpenRouter · Gemini)

---

## 🧠 Multi-Model Architecture

Each persona is assigned a specific AI provider, configured in `personas.yaml`:

| Provider key | Backend | Used for |
|---|---|---|
| `claude` | Anthropic Claude | Business assistant (streaming) |
| `groq` | Groq (llama-3.3-70b) | Analyst, Skeptic |
| `openrouter_deepseek` | DeepSeek via OpenRouter | Creative, Pragmatist |
| `openrouter_owl` | Owl Alpha via OpenRouter | *(configurable)* |
| `openrouter_gemini` | Gemini Lite via OpenRouter | Topic categorisation |
| `gemini` | Google Gemini direct | Moderator *(optional)* |

All clients share a `CircuitBreaker` — after N consecutive failures the breaker opens, logs a warning, and Groq fallback (if configured) kicks in automatically.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) package manager
- 6 Telegram bots created via [@BotFather](https://t.me/BotFather)
- Telegram Premium (for Business feature)

### 1. Clone & install

```bash
git clone https://github.com/V128S/son_of_den.git
cd son_of_den
uv sync
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and fill in your tokens
```

Key variables:

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (business assistant) |
| `GROQ_API_KEY` | Groq API key (analyst & skeptic) |
| `OPENROUTER_API_KEY` | OpenRouter key (creative, pragmatist, topic classifier) |
| `GEMINI_API_KEY` | Google Gemini key *(optional — moderator)* |
| `BUSINESS_BOT_TOKEN` | Business auto-responder bot token |
| `PANEL_BOT_ANALYST_TOKEN` | Panel analyst bot token |
| `PANEL_BOT_SKEPTIC_TOKEN` | Panel skeptic bot token |
| `PANEL_BOT_CREATIVE_TOKEN` | Panel creative bot token |
| `PANEL_BOT_PRAGMATIST_TOKEN` | Panel pragmatist bot token |
| `PANEL_BOT_MODERATOR_TOKEN` | Panel moderator bot token |
| `PANEL_CHAT_ID` | Forum group chat ID (negative number) |
| `ADMIN_USER_ID` | Your Telegram user ID |
| `CLAUDE_MODEL` | Claude model (default: `claude-sonnet-4-6`) |
| `GROQ_MODEL` | Groq model (default: `llama-3.3-70b-versatile`) |
| `DEEPSEEK_MODEL` | DeepSeek model via OpenRouter |
| `OWL_ALPHA_MODEL` | Owl Alpha model via OpenRouter |
| `GEMINI_LITE_MODEL` | Gemini Lite model via OpenRouter (topic classifier) |
| `GEMINI_MODEL` | Gemini model for direct API (default: `gemini-2.0-flash`) |
| `MODERATOR_PROVIDER` | Provider for moderator persona (default: `claude`) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to Google credentials JSON *(optional)* |
| `GOOGLE_CALENDAR_ID` | Google Calendar ID (default: `primary`) |
| `USER_TIMEZONE` | Timezone for calendar events (default: `Europe/Moscow`) |
| `PERSONAS_PATH` | Path to personas YAML (default: `personas.yaml`) |
| `LOG_LEVEL` | Logging level (default: `INFO`) |

### 3. Set up bots

**Business bot:**
- In BotFather: `/setprivacy → Disable`
- In Telegram: *Settings → Telegram Business → Chatbots* → connect the bot
- The bot also responds to direct private messages from the admin

**Panel bots:**
- Add all 5 to your panel group (must be a **Forum** supergroup) as **admins**
- Get the group ID via `@RawDataBot` → put in `PANEL_CHAT_ID`
- Topics are created and named automatically per discussion category

### 4. Run

```bash
uv run python -m claudebots
```

You should see: `Starting polling on 6 bots`

---

## 📩 Contact Topics (Business Bot)

When the business bot receives a message from a contact, it:

1. Creates a **forum topic** named `💬 ContactName` in your chat
2. Mirrors every incoming message as `📩 Name:\n<text>`
3. Mirrors every auto-reply as `🤖 Ответ:\n<text>`

You can **write directly inside a contact's topic** — the bot recognises you as the owner and answers your questions with full context of that contact's recent messages.

---

## 🗂 Owner Topic Categorisation

When the owner writes a message to the business bot (in any forum topic or general chat), the bot:

1. Classifies the message into one of the fixed categories using AI
2. Routes the message to the matching topic thread (or creates one if missing)
3. Renames the auto-created topic to the category name asynchronously

Available categories:

| Emoji | Category |
|---|---|
| 📋 | Задачи |
| 💡 | Идеи |
| 📊 | Аналитика |
| 🗓 | Планирование |
| 👥 | Клиенты |
| 💰 | Финансы |
| 📢 | Маркетинг |
| 🔧 | Технологии |
| 📝 | Разное |

---

## 🗓 Google Calendar Integration *(optional)*

The business assistant reads your calendar in real time to answer questions like *"when is the dinner?"* or *"is there time for a call on Friday?"*

**Setup:**
1. Create a Service Account in [Google Cloud Console](https://console.cloud.google.com)
2. Enable **Google Calendar API**
3. Download the JSON key → place it in the project root
4. Share your calendar with the service account email (read-only)
5. Set in `.env`:
   ```env
   GOOGLE_SERVICE_ACCOUNT_FILE=google_credentials.json
   GOOGLE_CALENDAR_ID=primary
   USER_TIMEZONE=Europe/Moscow
   ```

---

## 🧪 Testing

```bash
uv run pytest                  # all 68 tests
uv run pytest tests/unit       # unit tests only (fast)
uv run pytest tests/integration
```

---

## ⚙️ Admin Commands

Send to any of the 6 bots (admin user only):

| Command | Description |
|---|---|
| `/ping` | Health check — bot replies `pong` |
| `/reset` | Clear conversation history for current chat/topic |
| `/cost` | Token usage + approximate USD spend across all providers |
| `/reload` | Hot-reload `personas.yaml` without restart |

---

## 📁 Project Structure

```
claudebots/
├── core/
│   ├── config.py            # Settings from .env (pydantic)
│   ├── personas.py          # Persona model + YAML loader
│   ├── ai_registry.py       # Multi-model client router
│   ├── conversation.py      # In-memory chat history (ring buffer)
│   ├── circuit_breaker.py   # Failure detection & auto-fallback
│   ├── alerts.py            # Throttled admin notifications
│   ├── calendar_client.py   # Google Calendar integration
│   ├── claude_client.py     # Anthropic API wrapper (streaming)
│   ├── groq_client.py       # Groq API wrapper
│   ├── gemini_client.py     # Google Gemini wrapper
│   └── openrouter_client.py # OpenRouter wrapper
├── routers/
│   ├── business.py          # Business + personal message handler
│   ├── panel.py             # Panel round orchestrator + forum topics
│   └── admin.py             # Admin commands
├── bots.py                  # Bot instances factory
└── __main__.py              # Entrypoint & dependency injection
```

---

## 🖥 Deploy on VPS

```bash
sudo cp deploy/telegram-claude-bots.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-claude-bots
journalctl -u telegram-claude-bots -f
```

---

## 📝 License

Private repository. All rights reserved.
