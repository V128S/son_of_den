<div align="center">

# 🤖 Son of Den — Telegram Automation Suite

**Personal assistant + multi-bot discussion system for Telegram**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?style=flat&logo=telegram)](https://core.telegram.org/bots)
[![Tests](https://img.shields.io/badge/Tests-315%20passing-brightgreen?style=flat)](#testing)
[![License](https://img.shields.io/badge/License-Private-red?style=flat)](#)

</div>

---

## ✨ What it does

**Business Assistant** — auto-responds to Telegram messages on behalf of the owner. Understands context, reads Google Calendar in real time, and handles any question politely while the owner is busy. Incoming messages are mirrored to the admin in a dedicated forum topic per contact.

**Personal Assistant** — the owner can write directly to the business bot in any forum topic. Messages are automatically classified into thematic categories (Tasks, Ideas, Planning, Clients, etc.) and routed to the matching topic — or a new one is created and named accordingly.

**Instagram Downloader** — send any public Instagram post/Reel/carousel URL and the bot instantly downloads photos or video and delivers them to a dedicated `📸 Instagram` forum topic. Works from both DM and supergroup.

**YouTube Audio** — send any `https://youtu.be/` or `https://youtube.com/watch?v=` link and the bot extracts the best-quality audio track and delivers it to a `🎵 YouTube` forum topic as a playable audio message (or document if >50 MB).

**TikTok & X/Twitter Downloader** — send any public TikTok or X/Twitter URL and the bot downloads the video and delivers it to a dedicated `🎬 TikTok` or `🐦 X / Twitter` forum topic.

**Panel Discussion** — 5 bots that debate any topic: Analyst, Skeptic, Creative, Pragmatist, and a Moderator who synthesises everything into a clean takeaway. Questions are automatically categorised and routed to thematic forum threads.

**Daily News Panel** — every morning the bot fetches yesterday's top headlines via Exa and automatically launches a panel discussion. Configurable time and interests.

**Daily Feed Digest** — one AI-written editorial summary of all configured Telegram channel posts from the past 24 h, delivered to the panel forum at a configured time.

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

**Services:**
- `InstagramDownloader` — yt-dlp powered downloader for public posts/Reels, with ffmpeg H.264 re-encode and carousel support
- `YTDownloader` — yt-dlp powered audio extractor for YouTube videos (no re-encode, native quality); also has `fetch_transcript()` for video summarisation
- `SocialDownloader` — yt-dlp powered downloader for TikTok and X/Twitter public videos

---

## 🧠 Multi-Model Architecture

Each persona is assigned a specific AI provider, configured in `personas.yaml`:

| Provider key | Backend | Used for |
|---|---|---|
| `claude` | Anthropic Claude | Business assistant (streaming, circuit breaker + Groq fallback) |
| `groq` | Groq llama-3.3-70b | Analyst, Skeptic; silent fallback for other providers |
| `openrouter_deepseek` | DeepSeek via OpenRouter | Creative, Pragmatist |
| `openrouter_owl` | Owl Alpha via OpenRouter | *(configurable in personas.yaml)* |
| `openrouter_gemini` | Gemini Lite via OpenRouter | Topic categorisation |
| `openrouter_nemotron` | Nvidia Nemotron via OpenRouter | *(configurable in personas.yaml)* |
| `openmodel` | deepseek-v4-flash (free) | Panel discussion, daily digest |
| `gemini` | Google Gemini direct API | Moderator *(optional alternative)* |

`ClaudeClient` has a built-in `CircuitBreaker`. All OpenRouter/OpenModel clients are wrapped with `FallbackClient` → Groq so any provider outage is handled silently.

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

**Required:**

| Variable | Description |
|---|---|
| `BUSINESS_BOT_TOKEN` | Business auto-responder bot token |
| `PANEL_BOT_ANALYST_TOKEN` | Panel analyst bot token |
| `PANEL_BOT_SKEPTIC_TOKEN` | Panel skeptic bot token |
| `PANEL_BOT_CREATIVE_TOKEN` | Panel creative bot token |
| `PANEL_BOT_PRAGMATIST_TOKEN` | Panel pragmatist bot token |
| `PANEL_BOT_MODERATOR_TOKEN` | Panel moderator bot token |
| `PANEL_CHAT_ID` | Forum group chat ID (negative number) |
| `ADMIN_USER_ID` | Your Telegram user ID |

**AI Providers (at least one panel provider required):**

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Claude (business assistant) |
| `GROQ_API_KEY` | Groq llama-3.3-70b (analyst, skeptic, fallback) |
| `OPENROUTER_API_KEY` | OpenRouter (creative, pragmatist, classifier, nemotron, owl) |
| `OPENMODEL_API_KEY` | OpenModel free deepseek-v4-flash (panel, daily digest) |
| `GEMINI_API_KEY` | Google Gemini direct API *(optional — moderator)* |
| `EXA_API_KEY` | Exa web search *(optional — daily news panel enrichment)* |

**Model overrides (all have sensible defaults):**

| Variable | Default |
|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-4-6` |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` |
| `DEEPSEEK_MODEL` | `deepseek/deepseek-v4-flash:free` |
| `OWL_ALPHA_MODEL` | `openrouter/owl-alpha` |
| `GEMINI_LITE_MODEL` | `google/gemini-3.1-flash-lite` |
| `NEMOTRON_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `OPENMODEL_MODEL` | `deepseek-v4-flash` |
| `GEMINI_MODEL` | `gemini-2.0-flash` |
| `MODERATOR_PROVIDER` | `claude` |

**Scheduling & notifications:**

| Variable | Default | Description |
|---|---|---|
| `USER_TIMEZONE` | `Europe/Moscow` | Timezone for all daily times |
| `MORNING_BRIEFING_TIME` | `09:00` | Daily AI briefing (calendar + panel memories) |
| `CONTACT_DIGEST_TIME` | `20:00` | Daily contact activity summary |
| `DAILY_NEWS_PANEL_TIME` | *(empty)* | Daily panel round from top news (Exa) |
| `DAILY_NEWS_INTERESTS` | *(falls back to `FEED_INTERESTS`)* | Topics for daily news search |
| `FEED_DIGEST_TIME` | *(empty)* | Daily editorial digest of channel posts |
| `CONTACT_FOLLOWUP_DAYS` | `0` | Days of silence before follow-up reminder (0 = off) |
| `DAILY_COST_ALERT_USD` | `0.0` | Daily budget alert threshold USD (0 = off) |
| `PANEL_REVIVAL_INTERVAL_HOURS` | `0.0` | Revival scheduler interval (0 = off) |

**Integrations (all optional):**

| Variable | Description |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to Google credentials JSON |
| `GOOGLE_CALENDAR_ID` | Google Calendar ID (default: `primary`) |
| `OBSIDIAN_VAULT_PATH` | Local Obsidian vault path for contact history |
| `SHEETS_PERSONAL_ID` | Google Sheet ID for price transfers |
| `SHEETS_MARKUP_PERCENT` | Markup % applied to transferred prices (default: `20`) |
| `METERS_SHEET_ID` | Google Sheet ID for utility meter readings |
| `EXPENSES_SHEET_ID` | Google Sheet ID for expense tracking |
| `FEED_CHANNELS` | Comma-separated Telegram channel slugs for RSS |
| `FEED_INTERESTS` | Topics for feed scoring (default: finance/crypto/AI/politics) |
| `PERSONAS_PATH` | Path to personas YAML (default: `personas.yaml`) |
| `LOG_LEVEL` | Logging level (default: `INFO`) |
| `LOG_FILE` | Log file path (empty = stdout only) |

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

## 📸 Instagram Downloader

Send any public Instagram URL to the bot — it will download the media and deliver it to a dedicated forum topic:

- **Posts & carousels** — photos and videos sent as media group
- **Reels** — video re-encoded to H.264/AAC with `faststart` for in-app playback
- Files >50 MB are sent as documents
- Works from both a private DM and directly in the supergroup
- Topic `📸 Instagram` is created automatically and reused across sessions
- Recovers automatically if the topic is manually deleted

```
https://www.instagram.com/p/ABC123/
https://www.instagram.com/reel/ABC123/
```

---

## 🎵 YouTube Audio

Send any YouTube video link — the bot extracts the best-quality audio and delivers it to `🎵 YouTube` topic:

- Downloads in native format (m4a/opus) — no re-encoding, quality preserved
- Files ≤ 50 MB → `send_audio` (playable in Telegram); larger → `send_document`
- Sends a private confirmation when the link came from DM and audio went to the supergroup topic
- Supported: `https://youtu.be/ID` and `https://youtube.com/watch?v=ID`
- Not supported: Shorts, playlists, channel pages, private videos

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
uv run pytest                  # all 315 tests (e2e excluded by default)
uv run pytest tests/unit       # unit tests only (fast, no external deps)
uv run pytest tests/integration
```

---

## ⚙️ Admin Commands

Send to any of the 6 bots (admin user only):

| Command | Description |
|---|---|
| `/ping` | Health check — bot replies `pong` |
| `/reset` | Clear conversation history for current chat/topic |
| `/cost` | Token usage + approximate USD spend (daily + all-time) |
| `/reload` | Hot-reload `personas.yaml` without restart |
| `/contacts` | List known contacts with mute status (🔇) |
| `/stats` | Contact count, panel topics/memories, token usage |
| `/panelfind <query>` | Search panel memories by text or topic (last 10 hits) |
| `/panelstatus` | Panel lock state, active round, pending reminders |
| `/personas` | List loaded personas with provider and model |
| `/panelschedule HH:MM Topic` | Schedule a one-off panel round at local time |
| `/panelcancel` | Cancel a pending scheduled panel round |
| `/panelbest` | Show the top-scored panel memory |
| `/panelworst` | Show the lowest-scored panel memory |

**Business bot topic commands** (typed inside a contact's forum topic):

| Command | Description |
|---|---|
| `/mute` or `/pause` | Pause AI auto-replies for this contact |
| `/unmute` or `/resume` | Resume AI auto-replies for this contact |

---

## 📁 Project Structure

```
claudebots/
├── core/
│   ├── config.py            # Settings from .env (pydantic-settings)
│   ├── personas.py          # Persona model + YAML loader (hot-reload)
│   ├── ai_registry.py       # Multi-model client router + FallbackClient
│   ├── scheduling.py        # Sleep-robust daily_at() scheduler (macOS fix)
│   ├── conversation.py      # In-memory chat history (ring buffer, maxlen=40)
│   ├── circuit_breaker.py   # Failure detection & auto-fallback
│   ├── alerts.py            # Throttled admin notifications
│   ├── calendar_client.py   # Google Calendar integration (60 s cache)
│   ├── claude_client.py     # Anthropic API wrapper (streaming + prompt cache)
│   ├── groq_client.py       # Groq API wrapper
│   ├── openmodel_client.py  # OpenModel deepseek-v4-flash (free)
│   ├── gemini_client.py     # Google Gemini direct API wrapper
│   ├── openrouter_client.py # OpenRouter wrapper (deepseek/owl/gemini/nemotron)
│   ├── feed_monitor.py      # RSS poller + feed digest scheduler
│   ├── search_client.py     # Exa web search enrichment
│   ├── obsidian_client.py   # Obsidian vault contact history
│   ├── sheets_client.py     # Google Sheets price transfer
│   ├── meters_client.py     # Utility meter readings → Sheets
│   └── state.py             # Atomic JSON persistence (bot_state.json)
├── routers/
│   ├── business.py          # Business + personal + media downloader handler
│   ├── panel.py             # Panel round orchestrator + reminder checker
│   ├── daily_news.py        # Once-a-day news panel (Exa → PanelRoundRunner)
│   ├── briefing.py          # Morning briefing scheduler
│   └── admin.py             # Admin commands (/ping, /cost, /panelschedule, …)
├── services/
│   ├── insta_downloader.py  # Instagram media downloader (yt-dlp + ffmpeg)
│   ├── yt_downloader.py     # YouTube audio extractor + transcript (yt-dlp)
│   └── social_downloader.py # TikTok & X/Twitter video downloader (yt-dlp)
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
