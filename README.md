# candy_v2

A Discord bot with per-server config (stored in Supabase), Groq-powered AI chat,
randomised fun action replies, a Question of the Day queue, and an opt-in
monthly server report built from activity and moderation statistics.

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

# Owner console (admin_web). Fails closed without ADMIN_EMAIL.
supabase secrets set ADMIN_EMAIL=you@example.com
supabase functions deploy admin-api --use-api
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
| `/config report` | Toggle, channel, schedule, timezone, modlog, dyno, staffroles |
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
| `/report generate` | Build a draft report for a month (previous month by default). |
| `/report preview` | Render the live numbers for a month without saving or posting anything. |
| `/report view` | Preview the stored report; posts a `.md` file when it is long. |
| `/report publish` | Post a report to the configured channel, or one you pass. Re-running it edits the existing message instead of posting a duplicate. |
| `/report edit` | Write or replace one human-written section. |
| `/report list` | Recent reports and their draft/published status. |
| `/report test` | Verify the setup and post a test message to the report channel. |
| `!log` | Manage Server only, in a `ticket…` channel. Summarises up to 1000 messages (plus the ticket bot's form) and posts `{channel}_{date}_{type}_log.json`; the same JSON, including the inferred ticket type, is stored in `guild_ticket_logs`. |

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

### Ticket logs (`!log`)

`!log` works only in channels whose name starts with `ticket`, and only for
members with Manage Server. It reads up to 1000 human messages (plus the ticket
bot's opening form, if it has one), asks the AI for a four-key summary
(`people_involved`, `when`, `why`, `ticket_type`), posts it as
`{channel}_{date}_{type}_log.json` (for example
`ticket-0001_2026-11-14_general-support_log.json`), and stores the same object
in `guild_ticket_logs`.

Every channel is named like `ticket-0001`, and the number restarts for each
ticket type, so the name cannot identify the type. The AI infers it from the
conversation and returns one of **General support**, **Reporting a user**,
**Partnership request**, **Custom role**, **Staff application** or **Other**.
A ticket bot's opening embed is included in what the AI reads, but the bot is
never listed as a person. The same label is copied into a `ticket_type` column,
and the monthly report shows the total tickets logged plus a count for every
type.

The transcript is chunked to fit the `chat` Edge Function's per-message limit.
Only the summary is stored — the transcript itself is never written to the
database or to disk.

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

## Monthly server report

An **opt-in** report for the previous calendar month, in the server's own
timezone. It is a plain markdown document with seven fixed sections:

1. Server Overview
2. Server Activity
3. Community Highlights
4. Staff & Moderation
5. Events & Activities
6. Issues & Improvements
7. Final Summary

Sections 1, 2, 4 and 5 are filled from collected statistics. Sections 3, 6 and
the prose in 7 are **human-written** and are stored on the report row, so they
survive regeneration. Nothing is posted automatically until an admin turns it on
**and** sets a channel.

### What is collected

| Data | How |
|---|---|
| Member count at month start/end | daily snapshot written by the activity flush; the actual snapshot date is shown when it is not the first/last day |
| Messages | counted per day, hour, channel and member (bots excluded) |
| Voice time | connected seconds per member and channel, including AFK |
| Joins, leaves, boosts | gateway events plus `premium_since` transitions |
| Warnings | parsed from **Dyno** mod-log embeds only |
| Bans, kicks, unbans, timeouts | Discord **audit log** |
| User reports | tickets whose AI-inferred type is “Reporting a user” |
| Tickets | `!log` summaries, grouped by the AI-inferred ticket type |
| Events | mirrored Discord scheduled events and participation counts |

Counters live **in memory** and are flushed in one RPC per guild (default every
5 minutes). A crash loses at most the current flush window; the bot does not
write to the database on every message.

Message and voice averages are divided by the days actually observed, not by
calendar days, so bot downtime does not look like a drop in activity. The report
also states how many days were observed and an approximate bot uptime.

Message and voice rankings are deliberately **separate** — there is no combined
"most active" score. Voice counts all connected time, including the AFK channel.

### Moderation sources, and the honest caveat

Warnings are only read from Dyno's mod-log embeds, because Discord's audit log
has no "warn" action. Everything else comes from the audit log. Dyno's
ban/kick embeds are ignored so the same action is not counted twice.

When an action is issued **through Dyno**, Discord records Dyno as the audit-log
actor. The report stores `via` (`native` or `dyno`) and reports that honestly
rather than guessing which human pressed the button. Warning embeds that cannot
be parsed are still stored with their raw embed and flagged `needs_review`.

### Setup

```
/config report channel #staff
/config report schedule 1 09:00      # day 1-28, local time
/config report timezone Asia/Kolkata # or UTC+5:30
/config report toggle true
```

Run `/report test` after configuring the channel. It walks the exact path a
scheduled report takes and stops at the first problem with a fixable message:
no channel set, the channel was deleted, missing permissions (it lists every
one), the database RPC is unreachable (was `schema.sql` applied?), or Discord
refused the message. On success it posts a short test message — with this
month's live counts — to the report channel and links to it, plus soft
warnings for things that will not block delivery (schedule still off, tracking
disabled, nothing collected yet).

`/config report status` shows the schedule, data sources and last generated
period. `/config report modlog` points at the channel where Dyno posts its logs;
`/config report dyno` changes which bot is treated as Dyno (default is Dyno's
public bot, `155149108183695360`). `/config report staffroles` marks which roles
count as staff.

Generate on demand with `/report generate`, fill in the narrative sections with
`/report edit`, and check the live numbers at any time with `/report preview`
(nothing is saved or posted). `/report view` shows the stored copy, and
`/report publish` posts it. Publishing again edits the message it already
posted instead of adding a duplicate, so an edit made after publishing is
visible where people already read the report.

Reports are stored in `guild_reports` as `auto_data` (numbers) plus `narrative`
(prose) plus the rendered markdown.

The scheduler generates only the **most recent** missed period. If the bot was
offline for several months, older periods are logged as skipped instead of
flooding the channel with a backlog. A failed delivery is retried after 30
minutes. Day 1–28 avoids the months that have no 29th–31st.

### Required permissions and intents

The bot needs, in addition to its existing permissions:

- **View Channels** and **Read Message History** (activity counts)
- **View Audit Log** (ban/kick/unban/timeout)
- **Send Messages**, **Embed Links**, **Attach Files** (report delivery)

The **Message Content** and **Server Members** intents are already required by
other features. The **Presence** intent is optional: without it online counts
are stored as `NULL` and the report omits them rather than showing a false zero.

### Retention and privacy

Per-member, per-channel and per-hour detail is pruned after about 400 days
(`REPORT_RETENTION_DAYS`). Daily totals, generated reports, boost events and
moderation events are kept. **No message text, attachment content or voice audio
is stored** — only counts, timestamps, channel names and display-name snapshots.
Deleting a dashboard account removes that user's per-member rows and boost
events, and clears their identifiers from moderation records; generated
reports are point-in-time documents and are not rewritten. This is disclosed
in the Privacy Policy (section 3.10).

Environment variables (all optional):

```
ACTIVITY_TRACKING_ENABLED=true   # false stops collection entirely
ACTIVITY_FLUSH_SECONDS=300       # 60-3600
REPORT_RETENTION_DAYS=400        # 30-3650
MODERATION_POLL_SECONDS=600      # 120-3600, needs View Audit Log
MODERATION_BACKFILL_DAYS=30      # first poll only, capped at 45 by Discord
```

Two disclosures the report makes explicit: "Members Left" includes kicks and
bans (the existing join/leave counter treats every removal as a leave), and the
first tracked month may have data gaps if tracking started mid-month.

---

## Project layout

```
main.py                   Entry point and startup order
config.py                 Environment configuration
supabase/schema.sql       ALL structure: tables, columns, indexes, RLS,
                          queue helpers, memory retention, logging,
                          AI stats, activity + report tables   (run 1st)
supabase/seed_replies.sql Fun action reply catalog        (run 2nd)

db/                       Supabase client, config cache, Realtime listener
services/                 Config writes, Groq client, rendering, QOTD scheduler,
                          host rotation reminder, activity tracker, moderation
                          tracker, report service, report scheduler
cogs/                     config, general, fun, qotd, ai, activity, report
utils/                    Event logging, snowflake IDs, timezones, log setup
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
