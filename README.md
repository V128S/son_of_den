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

**Business Assistant** — auto-responds to Telegram messages on behalf of the owner. Understands context, reads Google Calendar in real time, and handles any question politely while the owner is busy. Incoming messages and bot replies are mirrored to the admin in a dedicated forum topic per contact.

**Panel Discussion** — 5 bots that debate any topic you throw at them: Analyst, Skeptic, Creative, Pragmatist, and a Moderator who synthesises everything into a clean takeaway. Questions are automatically categorised and routed to thematic forum threads.

---

## 🏗 Architecture

```
One Python process · One asyncio loop · 6 Telegram bots

┌─────────────────┐    ┌──────────────────────────────────┐
│  Business Bot   │    │          Panel Bots               │
│  (auto-reply)   │    │  Analyst · Skeptic · Creative     │
│  + private DM   │    │  Pragmatist · Moderator           │
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
- `GoogleCalendarClient` — live schedule fetching with 60s cache
- `PersonaRegistry` — hot-reloadable YAML persona definitions
- `AlertSender` — throttled admin notifications
- `AIRegistry` — multi-model routing (Claude · Groq · OpenRouter · Gemini)

---

## 🧠 Multi-Model Architecture

Each persona is assigned a specific AI provider, configured in `personas.yaml`:

| Provider key | Backend | Used for |
|---|---|---|
| `claude` | Anthropic Claude | Business assistant (streaming) |
| `groq` | Groq (llama/mixtral) | Analyst, Skeptic |
| `openrouter_deepseek` | DeepSeek via OpenRouter | Creative, Pragmatist |
| `openrouter_owl` | Owl Alpha via OpenRouter | *(configurable)* |
| `openrouter_gemini` | Gemini Lite via OpenRouter | Topic categorisation |
| `gemini` | Google Gemini direct | Moderator *(optional)* |

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
| `ANTHROPIC_API_KEY` | Anthropic API key (for business assistant) |
| `GROQ_API_KEY` | Groq API key (for analyst & skeptic) |
| `OPENROUTER_API_KEY` | OpenRouter key (for creative, pragmatist, topic analysis) |
| `GEMINI_API_KEY` | Google Gemini key *(optional — for moderator)* |
| `BUSINESS_BOT_TOKEN` | Business auto-responder bot |
| `PANEL_BOT_*_TOKEN` | 5 panel bot tokens (analyst, skeptic, creative, pragmatist, moderator) |
| `PANEL_CHAT_ID` | Forum group chat ID (negative number) |
| `ADMIN_USER_ID` | Your Telegram user ID |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to Google credentials JSON *(optional)* |
| `GROQ_MODEL` | Groq model name (default: `llama-3.3-70b-versatile`) |
| `DEEPSEEK_MODEL` | DeepSeek model via OpenRouter |
| `GEMINI_MODEL` | Gemini model name |

### 3. Set up bots

**Business bot:**
- In BotFather: `/setprivacy → Disable`
- In Telegram: *Settings → Telegram Business → Chatbots* → connect the bot
- The bot will also respond to direct private messages from the admin

**Panel bots:**
- Add all 5 to your panel group (must be a **Forum** supergroup) as **admins**
- Get the group ID via `@RawDataBot` → put in `PANEL_CHAT_ID`
- Topics are created automatically per discussion category

### 4. Run

```bash
uv run python -m claudebots
```

You should see: `Starting polling on 6 bots`

---

## 📩 Contact Topics (Business Bot)

When the business bot receives a message from a contact, it:

1. Automatically creates a **forum topic** named `💬 ContactName` in your private chat with the bot
2. Mirrors every incoming message as `📩 Name:\n<text>`
3. Mirrors every auto-reply as `🤖 Ответ:\n<text>`

You can also **chat directly with the bot** inside a contact's topic — it will understand that you are the owner (Denis) and provide a brief conversation summary or answer your questions about that contact.

---

## 🗓 Google Calendar Integration *(optional)*

The business assistant can read your calendar in real time to answer questions like *"when is the dinner?"* or *"is there time for a call on Friday?"*

**Setup:**
1. Create a Service Account in [Google Cloud Console](https://console.cloud.google.com)
2. Enable **Google Calendar API**
3. Download the JSON key → place it in the project root
4. Share your calendar with the service account email (read-only)
5. Set in `.env`:
   ```env
   GOOGLE_SERVICE_ACCOUNT_FILE=google_credentials.json
   GOOGLE_CALENDAR_ID=primary
   USER_TIMEZONE=Europe/Kyiv
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
| `/reset` | Clear conversation history for current chat |
| `/cost` | Token usage + approximate USD spend |
| `/reload` | Hot-reload `personas.yaml` without restart |

---

## 📁 Project Structure

```
claudebots/
├── core/
│   ├── config.py           # Settings from .env
│   ├── personas.py         # Persona model + YAML loader
│   ├── ai_registry.py      # Multi-model client router
│   ├── conversation.py     # In-memory chat history
│   ├── circuit_breaker.py  # Failure detection & fallback
│   ├── alerts.py           # Throttled admin notifications
│   ├── calendar_client.py  # Google Calendar integration
│   ├── claude_client.py    # Anthropic API wrapper (streaming)
│   ├── groq_client.py      # Groq API wrapper
│   ├── gemini_client.py    # Google Gemini wrapper
│   └── openrouter_client.py # OpenRouter wrapper
├── routers/
│   ├── business.py         # Business + private message handler
│   ├── panel.py            # Panel round orchestrator + forum topics
│   └── admin.py            # Admin commands
└── __main__.py             # Entrypoint & DI wiring
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
