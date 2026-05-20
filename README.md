# telegram-claude-bots

Personal auto-responder + 5-bot panel discussion on Claude.

Spec: `docs/specs/2026-05-19-telegram-claude-bots-design.md`

## Setup

### 1. Create bots in @BotFather (one-time)

You need 6 bots. Each needs `/setprivacy → Disable` so they see all group messages,
and the panel ones must be added as admins to the panel group.

- `business_bot` — the auto-responder. Connect it via Settings → Telegram Business → Chatbots on your Premium account.
- `panel_analyst`, `panel_skeptic`, `panel_creative`, `panel_pragmatist`, `panel_moderator` — the 5 panelists.

### 2. Configure `.env`

    cp .env.example .env

Fill in:
- `ANTHROPIC_API_KEY`
- 6 bot tokens
- `PANEL_CHAT_ID` (negative number for the panel group; find by adding `@RawDataBot` once)
- `ADMIN_USER_ID` (your own Telegram user id)

### 3. Install + run

    uv sync
    uv run python -m claudebots

Watch the logs — you should see `Starting polling on 6 bots`.

## Tests

    uv run pytest                  # all unit + integration
    uv run pytest tests/unit       # quick: pure logic
    uv run pytest -m e2e           # reserved for live tests; off by default

## Admin commands

DM any of the 6 bots:
- `/ping` — alive check
- `/reset` — clear conversation history for the current chat
- `/cost` — token counters + approximate USD spend since process start
- `/reload` — re-read `personas.yaml` without restarting

## Editing personas

Edit `personas.yaml`, then send `/reload` to any bot. Broken YAML keeps the previous registry — bot stays alive.

## Manual acceptance checklist (run before your first "production")

- [ ] Connected `business_bot` via Settings → Telegram Business → Chatbots
- [ ] Set Selected Chats = one test contact
- [ ] Test contact sent a message → bot replied as Майя, "from bot" badge visible
- [ ] `/reset` in DM with test contact → next message starts fresh context
- [ ] Created panel group, added all 5 panel bots as admins
- [ ] Found group id via @RawDataBot → put in `PANEL_CHAT_ID`
- [ ] Sent a topic in panel → got 4 expert replies in order + moderator summary
- [ ] Bots do NOT reply to each other when I am silent
- [ ] Revoked Anthropic key temporarily → business sends Майя's fallback; panel sends moderator fallback
- [ ] `/cost` reports tokens > 0 and reasonable USD figure

## Deploy on VPS

See `deploy/telegram-claude-bots.service` for a systemd unit template.

    sudo cp deploy/telegram-claude-bots.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now telegram-claude-bots
    journalctl -u telegram-claude-bots -f

## Project layout

See spec section "Структура репозитория" for the canonical layout.
