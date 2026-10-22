# candy_v2

A Discord bot with per-server config (stored in Supabase), Groq-powered AI chat,
randomised fun action replies, and a Question of the Day queue.

The companion dashboard is a separate repo: `candy_web`. It reads and writes the
same Supabase project, so there is one schema and one source of truth.

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
#    supabase/schema.sql        -> EVERYTHING structural: tables, columns,
#                                  indexes, RLS, queue helpers, memory
#                                  retention, logging, AI stats
#    supabase/seed_replies.sql  -> the fun action reply catalog (content only)
#
#    Two files, both idempotent. schema.sql is self-sufficient: it needs no
#    migrations and can be re-run safely, on both fresh and existing databases.

# 4. Run
python main.py
```

---

## Deploying

The bot and the Edge Functions deploy **separately**.

**Bot** — bot-hosting.net pulls this repo. Set the variables from `.env.example`
in the host panel, then deploy.

**Edge Functions** — deployed from your machine, once. The bot calls only
`chat` for AI replies. The dashboard also calls `apply-guild-settings` (settings
writes), `sync-guilds` (guild and permission refresh) and `delete-account`
(self-service erasure).

```bash
supabase login
supabase link --project-ref <YOUR_PROJECT_REF>

# Secrets used by the functions, never by the bot process itself.
supabase secrets set GROQ_API_KEY=gsk_...
supabase secrets set BOT_SERVICE_KEY=<the bot's SUPABASE_SERVICE_KEY value>
supabase secrets set DISCORD_BOT_TOKEN=<the bot token>

supabase functions deploy chat --use-api
supabase functions deploy apply-guild-settings --use-api
supabase functions deploy sync-guilds --use-api
supabase functions deploy delete-account --use-api
```

`--use-api` bundles server-side and avoids Docker/Podman issues on
SELinux-enforcing hosts. The AI function must stay named **`chat`** — the bot
hardcodes `{SUPABASE_URL}/functions/v1/chat`.

`BOT_SERVICE_KEY` is the exact service-role key the bot sends. The function
matches it against the platform-injected `SUPABASE_SERVICE_ROLE_KEY` first and
falls back to this custom secret, because the CLI refuses to overwrite
`SUPABASE_`-prefixed names — if the injected value ever drifts from the bot's
env, this is the reconciliation lever. Rotate it whenever the bot's key rotates:

```bash
supabase secrets set BOT_SERVICE_KEY=<new key>
```

---

## Configuration

Everything is editable from **both** Discord and the dashboard. In Discord:

| Command | Purpose |
|---|---|
| `/config status` | Current server setup at a glance |
| `/config general` | Prefix, prefix commands toggle, log channel, log events, log toggle |
| `/config chat` | AI toggle, model, system prompt, channels, context, memory, memory window, ignored channels, cooldown, temperature, max tokens |
| `/config replies` | List, add, remove, toggle, reset fun action replies |
| `/config qotd` | Toggle, time, timezone, channel, role, days, add, list, remove, next, skip |
| `/config dev renewed` | Bot owner only. Resets the host-renewal reminder clock. |
All `/config` subcommands require **Manage Server**, except `/config dev renewed`
which requires being the **bot owner** — the host renewal is an account-level
concern, not a per-server setting. `/renewal` is top-level for the same reason:
an owner without Manage Server would not see it under `/config`.

---

## Commands

| Command | Notes |
|---|---|
| `/renewal` | Bot owner only. Host-renewal countdown with a reset button. |
| `/ask <prompt>` | Ask the AI. The dashboard has a prompt tester for the same AI path. |
| `@Candy <message>` | AI reply when the channel is allowed. |
| `!candy <message>` | Same as a mention, as a prefix command. |
| `!candyp <message>` | Personalized: also weighs the prompter's own recent history. |
| `!help` | The same role-aware menu as `/help`, as a text command. |
| `/hello`, `/help` | General. |
| `/hug`, `/punch`, `/kick`, ... | Fun actions. Every one also works as `!hug`, etc. |

Fun replies are chosen at random from a per-action list. Guilds can override the
defaults, and a guild override takes precedence over the catalog default.

Prefix examples above use the default `!`. The prefix is configurable per server
(`/config general prefix`), and `/help` and `!help` always show the server's live
value rather than assuming `!`.

Prefix commands are opt-out per guild and target the same code path as the slash
equivalents.

`!candyp` (the **p** is for **personalized**) is the same as `!candy` plus the
prompter's own recent turns. `!candyp` is checked before `!candy` with a word
boundary, so it can never be swallowed by its shorter sibling.

---

## Host renewal reminder

The bot runs on **bot-hosting.net's free tier**, which stops the bot unless the
plan is manually renewed **every 4 days**. Miss the window and the bot goes
offline until it is renewed.

**The bot cannot read the host's renewal timer** — that clock lives entirely on
bot-hosting's side. So the reminder measures the only clock the bot can observe:
**how long its process has been running**.

```
ROTATION_REMINDER_DAYS=3     # remind after 3 days of uptime; 0 disables
```

Three days against a four-day window leaves a day of headroom, so the reminder
arrives while there is still time to act.

The reminder is delivered as a **DM to the bot owner**, resolved from Discord
(`application_info().owner`) so no extra configuration is needed. The DM carries
a single **"I've renewed"** button: press it and the countdown restarts. If DMs
are closed the reminder falls back to each guild's log channel, and an
account-level row is written to `bot_events` for the dashboard feed.

`/config dev renewed` (bot owner only) does the same thing from slash commands,
for when the DM has scrolled away. `/renewal` shows the same clock on demand —
days running, the threshold, and the next reminder as a Discord relative
timestamp — and carries the same reset button.

A Discord button cannot both reset the clock (a callback) and open a URL (a link
button), so the reminder keeps two: **I've renewed** resets, and **Open
bot-hosting panel** navigates. After a reset the message is rewritten as a
confirmation with the panel link still attached, which is the closest a single
click can get to "renew and reset".

### The button survives restarts

The button is a **persistent view** — a fixed `custom_id` with no timeout — and
it is re-registered on every startup. This matters more than it looks: the
reminder DM sits in your inbox indefinitely, and the click that matters most
happens *right after* a restart, because renewing the host is what restarts the
bot. An ordinary button holding an in-memory reference would already be dead by
then. Once pressed, the button disables itself so it cannot be double-fired, and
only the bot owner can use it.

### Why uptime, and what that costs you

A renewal does *not* restart the process, so a pure uptime clock drifts. But it
drifts in the **safe direction**: a host restart (crash, platform reboot, deploy)
resets the clock, which can only make the reminder *later*, never earlier. The
failure mode that matters is "the bot is silently gone", so a slightly late
reminder after a restart is acceptable where a missing one is not.

If the host restarts more often than every three days, the clock never reaches
the threshold and no reminder fires. In that case lower `ROTATION_REMINDER_DAYS`
or treat the restarts themselves as the signal.

The boot time is recorded in a marker file beside the log file (`.boot_time`
under `logs/`). Deliberately, the marker is **not** rewritten on every start —
only a renewal acknowledgement (the button, or `/config dev renewed`) resets it —
otherwise a host that restarts hourly would never reach the threshold.

---

## AI behaviour

Each exchange produces **two independent records**:

| Table | Holds | Purpose |
|---|---|---|
| `ai_interactions` | a one-line **summary**, plus tokens and latency | dashboard usage stats. No message text, ever. |
| `ai_memory` | the **real prompt and reply** | memory that survives a restart. |

Memory is **global per server**: all channels share one pool, the way a person
remembers a room. Retrieval filters on `guild_id`, not `channel_id` — the channel
is stored on each row for the ignore list and for debugging, but it is not the
lookup key.

### Memory survives restarts

The bot host restarts every 4 days for renewal. The in-RAM history window dies with
the process, so on the first request after a restart the guild's recent turns are
**reloaded from `ai_memory`** — lazily, only for the guild actually being used. A
256 MB host must never preload every guild at startup.

Both halves of each exchange are stored, not just the reply: a reply with no prompt
cannot be replayed into a conversation. This also keeps token use flat, because the
stored shape is exactly the shape the model already receives.

> **Note on the original design.** The bot previously stored **no** message text
> anywhere, and `ai_interactions` still does not. `ai_memory` deliberately reverses
> that stance for this one table, because restart-safe continuity is impossible
> without the actual words. Users' messages are now in the database.

### Retention is a per-row window

```
ai_memory_days               # per guild, default 30; 0 = keep forever
/config chat memorydays 30
```

Every stored turn has a `created_at`. Pruning deletes rows **older than the window**:

```sql
delete from ai_memory where guild_id = $1 and created_at < now() - interval '30 days';
```

**Each row expires on its own 30th birthday.** A row written today dies in 30 days;
one written tomorrow dies in 31. The table is never cleared wholesale — there is no
"reset after 30 days" event, only a rolling window. This is deliberate: a whole-table
wipe would destroy today's conversations along with the old ones.

Pruning runs **on the same write pass** as the insert (via `ai_memory_prune()`), so
retention needs no scheduled job. Rows can only accumulate while the bot is up and
being messaged, which is exactly when it can prune.

### Per-channel opt-out

A channel can be excluded from memory entirely — nothing said there is recorded or
recalled:

```
/config chat memorychannels add #some-channel      # stop remembering here
/config chat memorychannels remove #some-channel   # remember here again
```

### Turning memory off

```
!memory off              # prefix, Manage Server required
/config chat memory off  # slash equivalent
```

Replace `!` with your server's prefix; `/help` shows the current one.

Memory off means the AI **neither reads nor writes** anything. It still answers, but
every reply is made with no knowledge of past chat, and nothing new is recorded.

**Nothing already stored is deleted.** Turning memory back on restores the previous
context immediately, and past usage stats survive. This is a deliberate choice:
"off" is meant to be a switch, not a shredder.

The flag is separate from `ai_context_messages`, because the two answer different
questions — that one is *how many* past messages to recall, this one is *whether* to
recall at all. Read depth is left untouched while memory is off, so re-enabling
restores the configured depth rather than resetting it to zero.

---

## Project layout

```
main.py                   Entry point and startup order
config.py                 Environment configuration
supabase/schema.sql       ALL structure: tables, columns, indexes, RLS,
                          queue helpers, memory retention, logging,
                          AI stats                       (run 1st)
supabase/seed_replies.sql Fun action reply catalog        (run 2nd)

db/                       Supabase client, config cache, Realtime listener
services/                 Config writes, Groq client, rendering, QOTD scheduler,
                          host rotation reminder
cogs/                     config, general, fun, qotd, ai
utils/                    Event logging, snowflake IDs, log setup
supabase/functions/chat/                  The bot's Groq proxy
supabase/functions/apply-guild-settings/  Dashboard settings write gate
supabase/functions/sync-guilds/           Dashboard guild/permission sync
supabase/functions/delete-account/        Dashboard account erasure
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
