# candy_v2

A Discord bot with per-server config (stored in Supabase), Groq-powered AI chat,
randomised fun action replies, and a Question of the Day queue.

The dashboard that edits the same config is a separate repo: `candy_web`.

---

## Architecture

```
Discord  ──>  candy_v2 (bot)  ──HTTPS──>  chat Edge Function (Supabase)  ──>  Groq API
                    │                              │
            service_role key              GROQ_API_KEY (secret)
```

**Supabase is the single source of truth.** Slash commands and the dashboard both
write to the same tables, and the bot subscribes to changes so config applies live.

**The Groq key is not on the bot.** It lives in Supabase secrets, read only by the
`chat` Edge Function. The bot host requires a manual renewal every 4 days; keeping
the key behind the Edge Function means AI survives bot downtime and never lands in
a host config dump or a database backup.

---

## Quick start

```bash
# 1. Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env      # then fill in DISCORD_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY

# 3. Create the database (run these IN ORDER in the Supabase SQL editor)
#    schema.sql                -> tables, RLS, QOTD queue functions
#    seed_replies.sql          -> the fun action reply catalog
#    migration_002_logging.sql -> logging, AI memory, activity feed

# 4. Run
python main.py
```

---

## Deploying

The bot and the Edge Function deploy **separately**.

**Bot** — bot-hosting.net pulls this repo. Set the variables from `.env.example`
in the host panel, then deploy.

**Edge Function** — must be deployed from your machine, once:

```bash
supabase login
supabase link --project-ref <YOUR_PROJECT_REF>
supabase secrets set GROQ_API_KEY=gsk_...
supabase functions deploy chat --use-api
```

`--use-api` bundles server-side and avoids Docker/Podman issues on
SELinux-enforcing hosts. The function must stay named **`chat`** — the bot
hardcodes `{SUPABASE_URL}/functions/v1/chat`.

---

## Configuration

Everything is editable from **both** Discord and the dashboard. In Discord:

| Command | Purpose |
|---|---|
| `/config status` | Current server setup at a glance |
| `/config general` | Prefix, prefix toggle, log channel, log events, log toggle |
| `/config chat` | AI toggle, model, system prompt, channels, context, cooldown, temperature, max tokens |
| `/config replies` | List, add, remove, toggle, reset fun action replies |
| `/config qotd` | Toggle, time, timezone, channel, role, days, add, list, remove, next, skip |

All `/config` subcommands require **Manage Server**.

---

## Commands

| Command | Notes |
|---|---|
| `/ask <prompt>` | Ask the AI. Also works on the dashboard. |
| `@Candy <message>` | AI reply when the channel is allowed. |
| `/hello`, `/help` | General. |
| `/hug`, `/punch`, `/kick`, ... | Fun actions. Every one also works as `!hug`, etc. |

Fun replies are chosen at random from a per-action list. Guilds can override the
defaults, and a guild override takes precedence over the catalog default.

Prefix commands are opt-out per guild and target the same code path as the slash
equivalents.

---

## AI behaviour

Each exchange asks the model for JSON: the user-facing `reply` and a one-line
`summary`. Only the **summary** is stored (`ai_interactions`), never the full
transcript — that keeps recall cheap, storage bounded, and user text minimal.

Memory is a rolling window of the newest ~50 summaries **per channel**, trimmed
as new ones arrive. Guild admins can see aggregate usage stats via
`ai_stats_for_guild()`, which returns counts only and has no message-text column.

---

## Project layout

```
main.py                   Entry point and startup order
config.py                 Environment configuration
schema.sql                Tables, RLS, QOTD queue functions   (run 1st)
seed_replies.sql          Fun action reply catalog            (run 2nd)
migration_002_logging.sql Logging, AI memory, activity feed   (run 3rd)

db/                       Supabase client, config cache, Realtime listener
services/                 Config writes, Groq client, rendering, QOTD scheduler
cogs/                     config, general, fun, qotd, ai
utils/                    Event logging, snowflake IDs, log setup
supabase/functions/chat/  The Groq proxy (deployed to Supabase, not the bot)
```

---

## Notes

- **Discord snowflakes** exceed JS `Number.MAX_SAFE_INTEGER`. IDs are `BIGINT`
  in Postgres and strings in JavaScript.
- **Templates** store `{user}` / `{target}` placeholders, never raw `<@id>`
  mentions. Enforced at write time.
- **`ai_channel_ids` empty means disabled**, not "all channels". There is a
  separate `ai_all_channels` flag.
- **Realtime** is not supported by the Python sync client; the bot falls back to
  polling every 15s automatically, so config changes still apply.
- **The `chat` Edge Function is gated by `verify_jwt: true`.** The platform
  rejects unauthenticated requests before the function runs, so the dashboard must
  call it through the Supabase JS client rather than a bare `fetch`.
