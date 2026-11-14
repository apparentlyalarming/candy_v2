-- ============================================================================
-- candy_v2 — Supabase schema (structure + migrations, single file)
-- ============================================================================
-- HOW TO RUN:
--   1. Open your Supabase project
--   2. SQL Editor -> New query
--   3. Paste this ENTIRE file
--   4. Run
--
-- This file is IDEMPOTENT. Running it twice will not error or duplicate data.
-- Safe to re-run after edits.
--
-- This file REPLACES the old split layout (schema.sql + migration_002/003/004).
-- Everything is merged here in dependency order so a fresh install needs ONE
-- paste. The migrations are gone; this is now the single source of truth for
-- structure. Reply content still lives in supabase/seed_replies.sql.
--
-- CRITICAL: create table IF NOT EXISTS IS A NO-OP ON AN EXISTING TABLE.
--   It never adds columns. That is why every added column below is ALSO
--   declared with `alter table ... add column if not exists` (section 02b).
--   A fresh install gets the columns from the create; an existing database
--   gets them from the alter. Never rely on the create alone for new columns.
--
-- SECTIONS:
--   01  Extensions
--   02  Tables
--   02b Column migrations (alter table ... add column if not exists)
--   03  Indexes
--   04  RLS helper functions  (can_manage_guild, is_dev, touch_updated_at)
--   05  Row Level Security policies
--   05b Atomic queue helpers
--   05c AI memory retention  (ai_memory_prune)
--   06  Realtime / replication
--   07  Logging / AI stats / activity feed  (ex-migration 002)
--   08  Verification queries
--
-- CRITICAL CONVENTION:
--   Discord IDs (guild/channel/role/user snowflakes) are BIGINT in Postgres.
--   They exceed JavaScript's Number.MAX_SAFE_INTEGER, so the web app MUST
--   always treat them as strings. The bot uses int() and JSON-safe handling.
-- ============================================================================


-- ============================================================================
-- 01  EXTENSIONS
-- ============================================================================

create extension if not exists "pgcrypto";


-- ============================================================================
-- 02  TABLES
-- ============================================================================

-- ---------------------------------------------------------------------------
-- guilds: one row per Discord server the bot knows about
-- ---------------------------------------------------------------------------
create table if not exists public.guilds (
    id          bigint primary key,                 -- Discord guild snowflake
    name        text        not null default 'Unknown',
    icon_url    text,
    bot_present boolean     not null default true,  -- is the bot currently in this guild
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

comment on table public.guilds is
    'One row per Discord guild. Written by the bot (service_role) and by the sync-guilds Edge Function.';


-- ---------------------------------------------------------------------------
-- guild_settings: all per-guild configuration (chat AI, QOTD, general)
-- ---------------------------------------------------------------------------
create table if not exists public.guild_settings (
    guild_id                bigint primary key references public.guilds(id) on delete cascade,

    -- general ---------------------------------------------------------------
    prefix                  text    not null default '!',
    prefix_enabled          boolean not null default true,      -- text commands (!hug, !candy)
    log_channel_id          bigint,
    log_enabled             boolean not null default true,      -- server event log on/off
    log_categories          text[]  not null default '{config,errors,qotd}',  -- which events to log

    -- chat AI ---------------------------------------------------------------
    ai_enabled              boolean not null default false,
    ai_model                text    not null default 'openai/gpt-oss-120b',
    ai_system_prompt        text    not null default 'You are Candy, a friendly and helpful Discord bot. Keep replies short, warm and casual.',
    ai_max_tokens           integer not null default 512,
    ai_temperature          numeric(3,2) not null default 0.70,
    ai_context_messages     integer not null default 10,
    ai_memory_enabled       boolean not null default true,   -- off = neither read nor write memory
    ai_memory_days          integer not null default 30,     -- per-row expiry window; 0 = keep forever
    ai_memory_ignored_channels bigint[] not null default '{}', -- channels never recorded or recalled
    ai_user_cooldown_seconds integer not null default 5,
    ai_all_channels         boolean not null default false,
    ai_channel_ids          bigint[] not null default '{}',   -- empty = DISABLED (unless ai_all_channels)

    -- question of the day ---------------------------------------------------
    qotd_enabled            boolean not null default false,
    qotd_channel_id         bigint,
    qotd_role_id            bigint,
    qotd_time               time,
    qotd_timezone           text    not null default 'UTC',   -- IANA name, e.g. Asia/Kolkata
    qotd_days               integer[] not null default '{}',  -- 1=Mon .. 7=Sun; empty = every day
    qotd_empty_queue_notice boolean not null default false,   -- notify if queue is empty at fire time
    qotd_last_fired_at      timestamptz,                      -- restart double-fire guard

    -- monthly report + activity tracking ------------------------------------
    report_enabled          boolean not null default false,   -- auto-post the monthly report
    report_channel_id       bigint,                           -- where the report is posted
    report_day              smallint not null default 1,      -- day of month, 1..28
    report_time             time not null default '09:00',
    report_timezone         text not null default 'UTC',      -- IANA name or fixed offset
    report_last_generated_period date,                        -- period_start of the last auto-post
    moderation_log_channel_id bigint,                         -- channel where Dyno posts mod logs
    dyno_user_id            bigint not null default 155149108183695360, -- Dyno's public bot id
    staff_role_ids          bigint[] not null default '{}',   -- roles counted as staff

    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),

    constraint guild_settings_ai_temperature_range
        check (ai_temperature >= 0 and ai_temperature <= 2),
    constraint guild_settings_ai_max_tokens_positive
        check (ai_max_tokens > 0 and ai_max_tokens <= 8192),
    constraint guild_settings_ai_context_range
        check (ai_context_messages >= 0 and ai_context_messages <= 50),
    constraint guild_settings_ai_memory_days_range
        check (ai_memory_days >= 0 and ai_memory_days <= 3650),
    constraint guild_settings_cooldown_range
        check (ai_user_cooldown_seconds >= 0 and ai_user_cooldown_seconds <= 3600),
    constraint guild_settings_qotd_days_range
        check (qotd_days <@ array[1,2,3,4,5,6,7]::integer[]),
    constraint guild_settings_report_day_range
        check (report_day >= 1 and report_day <= 28)
);

comment on table public.guild_settings is
    'Per-guild bot configuration. Single source of truth for both slash commands and the dashboard.';
comment on column public.guild_settings.prefix_enabled is
    'Whether text/prefix commands (!hug, !candy) are active. Slash commands are unaffected.';
comment on column public.guild_settings.ai_channel_ids is
    'Channels where AI responds. Empty array = disabled (see ai_all_channels for everywhere).';
comment on column public.guild_settings.ai_memory_enabled is
    'When false the AI neither reads nor writes memory. Existing ai_memory rows are left untouched, so re-enabling restores prior context.';
comment on column public.guild_settings.ai_memory_days is
    'How many days of AI memory to keep, per row (each row expires on its own Nth birthday). 0 = keep forever. Default 30.';
comment on column public.guild_settings.ai_memory_ignored_channels is
    'Channels the AI neither records nor recalls. Per-channel escape hatch for channels where memory must stay off.';
comment on column public.guild_settings.qotd_days is
    'Days of week QOTD fires: 1=Mon..7=Sun. Empty array = every day.';
comment on column public.guild_settings.log_channel_id is
    'Channel where the bot posts server event logs. NULL = logging off for this guild.';
comment on column public.guild_settings.log_categories is
    'Which event categories to log: config, commands, qotd, errors, members, ai.';


-- ---------------------------------------------------------------------------
-- user_guilds: which guilds a user can manage (drives ALL dashboard RLS)
-- ---------------------------------------------------------------------------
create table if not exists public.user_guilds (
    user_id     uuid    not null references auth.users(id) on delete cascade,
    guild_id    bigint  not null references public.guilds(id) on delete cascade,
    permissions bigint  not null default 0,        -- raw Discord permission bitmask
    is_admin    boolean not null default false,    -- ADMINISTRATOR (bit 8)
    synced_at   timestamptz not null default now(),
    primary key (user_id, guild_id)
);

comment on table public.user_guilds is
    'Written ONLY by the sync-guilds / apply-guild-settings Edge Functions and the bot. Rows expire after 10 minutes (see can_manage_guild), so this is a cache of a recent Discord answer, not a standing grant.';

comment on column public.user_guilds.synced_at is
    'When Discord last confirmed this membership. can_manage_guild() refuses rows older than 10 minutes.';

-- ---------------------------------------------------------------------------
-- profiles: auth user <-> Discord snowflake
-- ---------------------------------------------------------------------------
-- needed for a mapping the bot can use: auth.users is not exposed through
-- PostgREST, so the bot cannot join a member_remove event (which carries a
-- Discord ID) to a user_guilds row (keyed by uuid) without this.
create table if not exists public.profiles (
    user_id         uuid primary key references auth.users(id) on delete cascade,
    discord_user_id bigint not null unique,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

comment on table public.profiles is
    'Written by the Edge Functions (service_role) from the caller''s Discord identity. Used by the bot to revoke dashboard access on leave/demotion.';


-- ---------------------------------------------------------------------------
-- reply_actions: shipped catalog of fun actions (NOT user editable)
-- ---------------------------------------------------------------------------
create table if not exists public.reply_actions (
    key               text primary key,             -- 'hug', 'kick', ...
    label             text not null,                -- 'Hug'
    emoji             text,                         -- optional display emoji
    default_templates text[] not null default '{}',
    is_guild_addable  boolean not null default false, -- reserved for future custom actions
    sort_order        integer not null default 0,
    created_at        timestamptz not null default now()
);

comment on table public.reply_actions is
    'Global shipped catalog of fun actions with default reply templates. Public read, no user writes. Rows are seeded by supabase/seed_replies.sql.';
comment on column public.reply_actions.default_templates is
    'Templates use {user} and {target} placeholders. NEVER store raw <@id> mentions.';


-- ---------------------------------------------------------------------------
-- guild_replies: per-guild overrides for fun action replies
-- ---------------------------------------------------------------------------
create table if not exists public.guild_replies (
    guild_id  bigint  not null references public.guilds(id) on delete cascade,
    action    text    not null references public.reply_actions(key) on delete cascade,
    templates text[]  not null default '{}',
    enabled   boolean not null default true,
    updated_at timestamptz not null default now(),
    primary key (guild_id, action)
);

comment on table public.guild_replies is
    'Per-guild reply template override for one action. Absent row or empty templates = fall back to reply_actions.default_templates.';


-- ---------------------------------------------------------------------------
-- qotd_questions: FIFO queue of questions
-- ---------------------------------------------------------------------------
create table if not exists public.qotd_questions (
    id               bigserial primary key,
    guild_id         bigint not null references public.guilds(id) on delete cascade,
    question         text   not null,
    position         integer not null default 0,   -- FIFO order, lower = sooner
    created_by       uuid references auth.users(id) on delete set null,
    created_at       timestamptz not null default now(),
    asked_at         timestamptz,                  -- NULL = still queued
    asked_message_id bigint,                       -- Discord message id once asked
    constraint qotd_questions_question_not_blank
        check (length(btrim(question)) > 0)
);

comment on table public.qotd_questions is
    'QOTD FIFO queue. asked_at IS NULL = queued. Rows are marked asked, not deleted, so history survives.';


-- ---------------------------------------------------------------------------
-- ai_memory: real conversation turns for restart-safe AI memory
-- ---------------------------------------------------------------------------
-- ONE row per exchange, both halves NOT NULL. A reply with no prompt cannot be
-- fed back into a conversation, so a half-row is a bug rather than something to
-- tolerate.
--
-- GLOBAL PER GUILD: retrieval filters on guild_id, not channel_id. channel_id is
-- kept for the per-channel ignore list and for debugging, but the bot recalls the
-- whole server the way a person recalls a room.
--
-- This deliberately REVERSES the original no-transcripts stance. ai_interactions
-- still stores no message text; that table is analytics, this one is memory.

create table if not exists public.ai_memory (
    id                  bigserial primary key,
    guild_id            bigint not null references public.guilds(id) on delete cascade,
    channel_id          bigint,                     -- where it happened; NOT the lookup key
    user_id             uuid references auth.users(id) on delete set null,
    discord_user_id     bigint,                     -- the prompt author (drives !candyp)
    user_display_name   text,

    user_content        text not null,              -- what the user said
    assistant_content   text not null,              -- what the bot replied

    created_at          timestamptz not null default now(),

    constraint ai_memory_user_content_not_blank
        check (length(btrim(user_content)) > 0),
    constraint ai_memory_assistant_content_not_blank
        check (length(btrim(assistant_content)) > 0)
);

comment on table public.ai_memory is
    'Real AI conversation turns (prompt + reply) for restart-safe memory. Global per guild: retrieval filters on guild_id. One row per exchange.';
comment on column public.ai_memory.channel_id is
    'Stored for the per-channel ignore list and for debugging. Retrieval is by guild_id, not this column.';
comment on column public.ai_memory.discord_user_id is
    'Discord snowflake of the prompt author. Drives the personalized !candyp lookup.';
comment on column public.ai_memory.user_content is
    'The user prompt. Stored deliberately: a reply is uninterpretable without the prompt it answers.';


-- ---------------------------------------------------------------------------
-- ai_interactions: AI exchange SUMMARIES for analytics (never full text)
-- ---------------------------------------------------------------------------
-- Stores a SHORT summary per exchange, not the full transcript. Memory recall
-- uses ai_memory instead; this table exists so the dashboard can show usage
-- stats without ever exposing what anyone said.
--
-- RLS: a user can read their OWN rows across ALL servers. That is why user_id
-- references auth.users directly rather than being guild-scoped.

create table if not exists public.ai_interactions (
    id          bigserial primary key,
    guild_id    bigint references public.guilds(id) on delete cascade,
    channel_id  bigint,
    user_id     uuid references auth.users(id) on delete cascade,
    discord_user_id bigint,                       -- denormalised for log reads
    user_display_name text,

    summary     text not null,                    -- the stored memory line
    model       text,
    prompt_tokens   integer,
    completion_tokens integer,
    latency_ms  integer,
    succeeded   boolean not null default true,
    error_kind  text,

    created_at  timestamptz not null default now(),

    constraint ai_interactions_summary_length check (length(summary) <= 500)
);

comment on table public.ai_interactions is
    'AI exchange summaries used for analytics. Full transcripts are never stored here - see ai_memory for conversation turns.';
comment on column public.ai_interactions.summary is
    'One-line gist of the exchange. NOT the full reply text.';


-- ---------------------------------------------------------------------------
-- bot_events: dashboard activity feed (mirrors the Discord log channel)
-- ---------------------------------------------------------------------------
-- "Notable events only": config changes, command usage, QOTD fires, errors,
-- member joins/leaves, AI failures. Not routine chatter.

create table if not exists public.bot_events (
    id          bigserial primary key,
    guild_id    bigint references public.guilds(id) on delete cascade,
    category    text not null,
    title       text not null,
    description text,
    actor_id    bigint,
    actor_name  text,
    channel_id  bigint,
    level       text not null default 'info',     -- info | warning | error
    metadata    jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),

    constraint bot_events_category_valid
        check (category in ('config','commands','qotd','errors','members','ai')),
    constraint bot_events_level_valid
        check (level in ('info','warning','error'))
);

comment on table public.bot_events is
    'Activity feed surfaced on the dashboard. Mirrors what goes to the Discord log channel.';


-- ---------------------------------------------------------------------------
-- dev_users: allowlist for elevated dashboard reads
-- ---------------------------------------------------------------------------
-- An allowlist TABLE rather than a config value, so granting is a data change
-- rather than a deploy.

create table if not exists public.dev_users (
    user_id    uuid primary key references auth.users(id) on delete cascade,
    note       text,
    created_at timestamptz not null default now()
);

comment on table public.dev_users is
    'Users allowed to read every ai_interactions row via is_dev(). Grant sparingly.';


-- ---------------------------------------------------------------------------
-- user_preferences: per-account dashboard settings (theme, etc.)
-- ---------------------------------------------------------------------------
-- The dashboard stores the chosen theme in localStorage so it applies before
-- login, and mirrors it here when the user is signed in so a second device
-- picks it up instead of asking again. One row per user; only the owner can
-- read or write it.

create table if not exists public.user_preferences (
    user_id    uuid primary key references auth.users(id) on delete cascade,
    theme      text not null default 'light',
    updated_at timestamptz not null default now(),
    constraint user_preferences_theme_valid
        check (theme in ('light','glass','contrast'))
);

comment on table public.user_preferences is
    'Per-account dashboard preferences (currently the UI theme). Owner-only via RLS.';


-- ============================================================================
-- 02b  COLUMN MIGRATIONS
-- ============================================================================
-- Every column added after the original release is repeated here as an
-- `add column if not exists`. This is what makes the file work on an EXISTING
-- database: create table if not exists skips the whole column list, so the
-- alter statements are the only thing that can add a column to a live table.
--
-- Keep this section in sync with the create statements above.

alter table public.guild_settings
    add column if not exists log_enabled    boolean not null default true,
    add column if not exists log_categories text[]  not null default '{config,errors,qotd}';

-- MISSING SINCE THE ORIGINAL RELEASE. The bot has always written and read
-- guild_settings.prefix_enabled (/config general prefixcommands, the status
-- line, and the !hug/!candy gate in general_cog), but no schema file ever
-- created the column. On a database that never had it, every write failed with
-- 42703 and the status line silently reported the DEFAULT ("prefix commands:
-- on") no matter what an admin saved. Adding it here is a no-op where the
-- column already exists and creates it everywhere else.
alter table public.guild_settings
    add column if not exists prefix_enabled boolean not null default true;

alter table public.guild_settings
    add column if not exists ai_memory_enabled boolean not null default true;

alter table public.guild_settings
    add column if not exists ai_memory_days integer not null default 30;

alter table public.guild_settings
    add column if not exists ai_memory_ignored_channels bigint[] not null default '{}';

-- Monthly report + activity tracking (added 2026-11-10). Repeated here because
-- `create table if not exists` is a no-op on an existing database.
alter table public.guild_settings
    add column if not exists report_enabled          boolean  not null default false,
    add column if not exists report_channel_id       bigint,
    add column if not exists report_day              smallint not null default 1,
    add column if not exists report_time             time     not null default '09:00',
    add column if not exists report_timezone         text     not null default 'UTC',
    add column if not exists report_last_generated_period date,
    add column if not exists moderation_log_channel_id bigint,
    add column if not exists dyno_user_id            bigint   not null default 155149108183695360,
    add column if not exists staff_role_ids          bigint[] not null default '{}';

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'guild_settings_report_day_range'
    ) then
        alter table public.guild_settings
            add constraint guild_settings_report_day_range
            check (report_day >= 1 and report_day <= 28);
    end if;
end $$;

-- The QOTD queue index predates the uniqueness requirement, so an existing
-- database still has the non-unique version and `create unique index if not
-- exists` above would silently skip it. Drop and rebuild so the live queue
-- gains the constraint that stops two admins inserting the same position.
-- Safe to re-run; it is dropped by the same name whether or not it was unique.
drop index if exists public.qotd_questions_queue_idx;

create unique index if not exists qotd_questions_queue_idx
    on public.qotd_questions (guild_id, position)
    where asked_at is null;

-- Constrain log categories to the known set (guarded: constraint creation is
-- not idempotent the way add column is).
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'guild_settings_log_categories_valid'
    ) then
        alter table public.guild_settings
            add constraint guild_settings_log_categories_valid
            check (log_categories <@ array['config','commands','qotd','errors','members','ai']::text[]);
    end if;
end $$;

-- Range: 0 = keep forever, otherwise up to 10 years. Bounded so a typo cannot
-- ask for a million-day window.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'guild_settings_ai_memory_days_range'
    ) then
        alter table public.guild_settings
            add constraint guild_settings_ai_memory_days_range
            check (ai_memory_days >= 0 and ai_memory_days <= 3650);
    end if;
end $$;

-- Default AI model moved to gpt-oss-120b (2026-10-20). `set default` only
-- affects FUTURE inserts; the `update` treats rows still sitting on the old
-- default as never-customized, so existing servers pick up the new default
-- too. Both statements are idempotent, and a guild that chose a model via
-- `/config chat model` keeps whatever it picked (only the old default value
-- is migrated).
alter table public.guild_settings
    alter column ai_model set default 'openai/gpt-oss-120b';

update public.guild_settings
    set ai_model = 'openai/gpt-oss-120b'
    where ai_model = 'llama-3.3-70b-versatile';


-- ============================================================================
-- 03  INDEXES
-- ============================================================================

-- Fast "next unasked question" lookup + ordering.
-- UNIQUE on purpose: two admins adding a question at the same moment could
-- otherwise compute the same max(position) and both insert it, leaving the
-- queue with duplicate positions and an arbitrary order. The index also serves
-- the lookup it replaced, so it doubles as the ordering index.
create unique index if not exists qotd_questions_queue_idx
    on public.qotd_questions (guild_id, position)
    where asked_at is null;

-- Asked-history lookups
create index if not exists qotd_questions_asked_idx
    on public.qotd_questions (guild_id, asked_at desc)
    where asked_at is not null;

-- Permission lookups used by can_manage_guild()
create index if not exists user_guilds_user_idx
    on public.user_guilds (user_id);

create index if not exists user_guilds_guild_idx
    on public.user_guilds (guild_id);

-- Guild listing
create index if not exists guilds_bot_present_idx
    on public.guilds (bot_present);

-- AI memory: guild-scoped newest-first reads (the common query), per-person
-- reads for !candyp, and the retention sweep by age.
create index if not exists ai_memory_guild_created_idx
    on public.ai_memory (guild_id, created_at desc);

create index if not exists ai_memory_guild_user_created_idx
    on public.ai_memory (guild_id, discord_user_id, created_at desc);

create index if not exists ai_memory_created_idx
    on public.ai_memory (created_at);

-- AI interactions (analytics)
create index if not exists ai_interactions_channel_idx
    on public.ai_interactions (guild_id, channel_id, created_at desc);

create index if not exists ai_interactions_user_idx
    on public.ai_interactions (user_id, created_at desc);

create index if not exists ai_interactions_guild_idx
    on public.ai_interactions (guild_id, created_at desc);

-- Activity feed
create index if not exists bot_events_guild_idx
    on public.bot_events (guild_id, created_at desc);

create index if not exists bot_events_category_idx
    on public.bot_events (guild_id, category, created_at desc);


-- ============================================================================
-- 04  RLS HELPER FUNCTIONS
-- ============================================================================

-- ---------------------------------------------------------------------------
-- updated_at auto-touch
-- ---------------------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists guilds_touch_updated_at on public.guilds;
create trigger guilds_touch_updated_at
    before update on public.guilds
    for each row execute function public.touch_updated_at();

drop trigger if exists guild_settings_touch_updated_at on public.guild_settings;
create trigger guild_settings_touch_updated_at
    before update on public.guild_settings
    for each row execute function public.touch_updated_at();

drop trigger if exists guild_replies_touch_updated_at on public.guild_replies;
create trigger guild_replies_touch_updated_at
    before update on public.guild_replies
    for each row execute function public.touch_updated_at();

drop trigger if exists user_preferences_touch_updated_at on public.user_preferences;
create trigger user_preferences_touch_updated_at
    before update on public.user_preferences
    for each row execute function public.touch_updated_at();

drop trigger if exists profiles_touch_updated_at on public.profiles;
create trigger profiles_touch_updated_at
    before update on public.profiles
    for each row execute function public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- can_manage_guild(gid): does the CURRENT USER manage this guild?
--
-- SECURITY DEFINER is REQUIRED. Without it, a policy on guilds that reads
-- user_guilds which itself references guilds causes infinite recursion and
-- every query 500s. This function is the one place that recursion is broken.
-- ---------------------------------------------------------------------------
create or replace function public.can_manage_guild(gid bigint)
returns boolean
language sql
security definer
stable
set search_path = public
as $$
    select exists (
        select 1
        from public.user_guilds ug
        where ug.guild_id = gid
          and ug.user_id = auth.uid()
          and ug.synced_at > (now() - interval '10 minutes')
          and (
                ug.is_admin
                or (ug.permissions & 32) = 32      -- 32 = MANAGE_GUILD
                or (ug.permissions & 8)  = 8       -- 8  = ADMINISTRATOR
              )
    );
$$;

comment on function public.can_manage_guild(bigint) is
    'True when auth.uid() has MANAGE_GUILD (32) or ADMINISTRATOR (8) in the given guild and permissions were verified within the last 10 minutes. SECURITY DEFINER breaks RLS recursion.';

-- Lock the helper down: only the RLS engine / authenticated users should call it.
revoke all on function public.can_manage_guild(bigint) from public;
grant execute on function public.can_manage_guild(bigint) to authenticated;


-- ---------------------------------------------------------------------------
-- is_dev(): is the current user on the dev allowlist?
--
-- DEFINE THIS BEFORE ai_memory_prune() and ai_trim_channel(), which CALL it.
-- It lives here (rather than in a migration) so this file is self-sufficient:
-- previously schema.sql called is_dev() while only migration_002 created it, so
-- a fresh install of schema.sql alone raised at runtime the first time pruning
-- ran. Keeping the definition above its callers removes that trap.
-- ---------------------------------------------------------------------------
create or replace function public.is_dev()
returns boolean
language sql
security definer
stable
set search_path = public
as $$
    select exists (
        select 1 from public.dev_users d where d.user_id = auth.uid()
    );
$$;

comment on function public.is_dev() is
    'True when the current user is on the dev allowlist. Used for elevated dashboard views and to authorize maintenance RPCs.';

revoke all on function public.is_dev() from public;
grant execute on function public.is_dev() to authenticated;


-- ============================================================================
-- 05  ROW LEVEL SECURITY
-- ============================================================================
-- Rule: RLS ON for every table. A table with RLS on and NO policy is deny-all,
-- which is exactly the safe default we want.

alter table public.guilds          enable row level security;
alter table public.guild_settings  enable row level security;
alter table public.user_guilds     enable row level security;
alter table public.reply_actions   enable row level security;
alter table public.guild_replies   enable row level security;
alter table public.qotd_questions  enable row level security;
alter table public.ai_memory       enable row level security;
alter table public.ai_interactions enable row level security;
alter table public.bot_events      enable row level security;
alter table public.dev_users       enable row level security;
alter table public.user_preferences enable row level security;
alter table public.profiles        enable row level security;

-- Belt and braces: also force RLS for table owners where applicable.
alter table public.guilds          force row level security;
alter table public.guild_settings  force row level security;
alter table public.user_guilds     force row level security;
alter table public.reply_actions   force row level security;
alter table public.guild_replies   force row level security;
alter table public.qotd_questions  force row level security;
alter table public.ai_memory       force row level security;
alter table public.ai_interactions force row level security;
alter table public.bot_events      force row level security;
alter table public.dev_users       force row level security;
alter table public.user_preferences force row level security;
alter table public.profiles       force row level security;

-- ---------------------------------------------------------------------------
-- guilds
-- ---------------------------------------------------------------------------
drop policy if exists "guilds_select_managed" on public.guilds;
create policy "guilds_select_managed"
    on public.guilds
    for select
    to authenticated
    using (public.can_manage_guild(id));

-- No insert/update/delete policies: only the bot (service_role) and the
-- sync-guilds Edge Function may write guild rows.

-- ---------------------------------------------------------------------------
-- guild_settings
-- ---------------------------------------------------------------------------
drop policy if exists "guild_settings_select_managed" on public.guild_settings;
create policy "guild_settings_select_managed"
    on public.guild_settings
    for select
    to authenticated
    using (public.can_manage_guild(guild_id));

-- No delete policy: settings rows are never deleted by users (cascade handles guild removal).

-- Writes are NOT granted to the browser at all. A stale JWT plus a stale
-- user_guilds row used to be enough to PATCH this table directly through
-- PostgREST, bypassing the dashboard entirely, for as long as the attacker
-- avoided triggering a sync. The only writer is now the apply-guild-settings
-- Edge Function, which asks Discord at the moment of the write.
revoke insert, update on public.guild_settings from anon, authenticated;
drop policy if exists "guild_settings_insert_managed" on public.guild_settings;
drop policy if exists "guild_settings_update_managed" on public.guild_settings;

-- ---------------------------------------------------------------------------
-- user_guilds
-- ---------------------------------------------------------------------------
drop policy if exists "user_guilds_select_own" on public.user_guilds;
create policy "user_guilds_select_own"
    on public.user_guilds
    for select
    to authenticated
    using (user_id = auth.uid());

-- No insert/update/delete policies on purpose. Only service_role writes here.
-- If a user could insert their own rows, they could grant themselves any guild.

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------
drop policy if exists "profiles_select_own" on public.profiles;
create policy "profiles_select_own"
    on public.profiles
    for select
    to authenticated
    using (user_id = auth.uid());

-- service_role only for writes: a user must not be able to claim another
-- Discord identity and inherit that person's access.
revoke insert, update, delete on public.profiles from anon, authenticated;

-- ---------------------------------------------------------------------------
-- reply_actions (public catalog: readable by everyone, writable by nobody)
-- ---------------------------------------------------------------------------
drop policy if exists "reply_actions_select_all" on public.reply_actions;
create policy "reply_actions_select_all"
    on public.reply_actions
    for select
    to authenticated, anon
    using (true);

-- ---------------------------------------------------------------------------
-- guild_replies
-- ---------------------------------------------------------------------------
drop policy if exists "guild_replies_select_managed" on public.guild_replies;
create policy "guild_replies_select_managed"
    on public.guild_replies
    for select
    to authenticated
    using (public.can_manage_guild(guild_id));

drop policy if exists "guild_replies_insert_managed" on public.guild_replies;
create policy "guild_replies_insert_managed"
    on public.guild_replies
    for insert
    to authenticated
    with check (public.can_manage_guild(guild_id));

drop policy if exists "guild_replies_update_managed" on public.guild_replies;
create policy "guild_replies_update_managed"
    on public.guild_replies
    for update
    to authenticated
    using (public.can_manage_guild(guild_id))
    with check (public.can_manage_guild(guild_id));

drop policy if exists "guild_replies_delete_managed" on public.guild_replies;
create policy "guild_replies_delete_managed"
    on public.guild_replies
    for delete
    to authenticated
    using (public.can_manage_guild(guild_id));

-- ---------------------------------------------------------------------------
-- qotd_questions
-- ---------------------------------------------------------------------------
drop policy if exists "qotd_select_managed" on public.qotd_questions;
create policy "qotd_select_managed"
    on public.qotd_questions
    for select
    to authenticated
    using (public.can_manage_guild(guild_id));

drop policy if exists "qotd_insert_managed" on public.qotd_questions;
create policy "qotd_insert_managed"
    on public.qotd_questions
    for insert
    to authenticated
    with check (public.can_manage_guild(guild_id));

drop policy if exists "qotd_update_managed" on public.qotd_questions;
create policy "qotd_update_managed"
    on public.qotd_questions
    for update
    to authenticated
    using (public.can_manage_guild(guild_id))
    with check (public.can_manage_guild(guild_id));

drop policy if exists "qotd_delete_managed" on public.qotd_questions;
create policy "qotd_delete_managed"
    on public.qotd_questions
    for delete
    to authenticated
    using (public.can_manage_guild(guild_id));

-- ---------------------------------------------------------------------------
-- ai_memory
-- ---------------------------------------------------------------------------
-- SELECT only, for guilds the caller manages. No insert/update/delete policy on
-- purpose: memory is written and pruned by the bot alone, and a dashboard never
-- edits a transcript.
drop policy if exists "ai_memory_select_managed" on public.ai_memory;
create policy "ai_memory_select_managed"
    on public.ai_memory
    for select
    to authenticated
    using (public.can_manage_guild(guild_id));

-- ---------------------------------------------------------------------------
-- ai_interactions
--
-- User policy: read rows that belong to you, in ANY guild. This is the
-- "users see their own across all servers" rule. A separate dev policy below
-- lets allowlisted users read every row.
-- ---------------------------------------------------------------------------
drop policy if exists "ai_interactions_select_own" on public.ai_interactions;
create policy "ai_interactions_select_own"
    on public.ai_interactions
    for select
    to authenticated
    using (user_id = auth.uid());

drop policy if exists "ai_interactions_select_dev" on public.ai_interactions;
create policy "ai_interactions_select_dev"
    on public.ai_interactions
    for select
    to authenticated
    using (public.is_dev());

-- No insert/update/delete policies: only the bot (service_role) writes here.
-- Users cannot fabricate or erase their own history through the API.

-- ---------------------------------------------------------------------------
-- bot_events: guild admins may read events for guilds they manage.
-- ---------------------------------------------------------------------------
drop policy if exists "bot_events_select_managed" on public.bot_events;
create policy "bot_events_select_managed"
    on public.bot_events
    for select
    to authenticated
    using (public.can_manage_guild(guild_id));

-- Writes are service_role only.

-- ---------------------------------------------------------------------------
-- dev_users: users may check whether THEY are a dev (needed for UI gating).
-- They may not enumerate the list.
-- ---------------------------------------------------------------------------
drop policy if exists "dev_users_select_self" on public.dev_users;
create policy "dev_users_select_self"
    on public.dev_users
    for select
    to authenticated
    using (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- user_preferences: a user reads and writes ONLY their own row.
-- ---------------------------------------------------------------------------
drop policy if exists "user_preferences_select_own" on public.user_preferences;
create policy "user_preferences_select_own"
    on public.user_preferences
    for select
    to authenticated
    using (user_id = auth.uid());

drop policy if exists "user_preferences_insert_own" on public.user_preferences;
create policy "user_preferences_insert_own"
    on public.user_preferences
    for insert
    to authenticated
    with check (user_id = auth.uid());

drop policy if exists "user_preferences_update_own" on public.user_preferences;
create policy "user_preferences_update_own"
    on public.user_preferences
    for update
    to authenticated
    using (user_id = auth.uid())
    with check (user_id = auth.uid());

drop policy if exists "user_preferences_delete_own" on public.user_preferences;
create policy "user_preferences_delete_own"
    on public.user_preferences
    for delete
    to authenticated
    using (user_id = auth.uid());


-- ============================================================================
-- 05b  ATOMIC QUEUE HELPERS
-- ============================================================================
-- Reordering from a browser is N round-trips and can interleave, corrupting
-- positions. These functions do the whole operation in ONE transaction.

-- Append a question to the end of the queue.
create or replace function public.qotd_add_question(
    p_guild_id bigint,
    p_question text
)
returns public.qotd_questions
language plpgsql
security invoker
as $$
declare
    v_next_position integer;
    v_row public.qotd_questions;
    v_attempt integer;
begin
    if not public.can_manage_guild(p_guild_id) then
        raise exception 'not authorized for guild %', p_guild_id
            using errcode = '42501';
    end if;

    if p_question is null or length(btrim(p_question)) = 0 then
        raise exception 'question must not be blank' using errcode = '22023';
    end if;

    -- Serialize concurrent adds for this guild. Two admins adding at the same
    -- instant would otherwise read the same max(position) and insert the same
    -- position. Locking existing queue rows makes the second caller wait and
    -- then re-read a correct max.
    --
    -- The lock is a no-op when the queue is empty (zero rows to lock), which is
    -- why the unique index on (guild_id, position) exists as the real guarantee.
    -- Retry once so that race resolves into the next free position instead of
    -- surfacing a raw 23505 to the admin.
    for v_attempt in 1..2 loop
        perform 1
          from public.qotd_questions
         where guild_id = p_guild_id
           for update;

        select coalesce(max(position), 0) + 1
          into v_next_position
          from public.qotd_questions
         where guild_id = p_guild_id;

        begin
            insert into public.qotd_questions (guild_id, question, position, created_by)
            values (p_guild_id, btrim(p_question), v_next_position, auth.uid())
            returning * into v_row;

            return v_row;
        exception
            when unique_violation then
                -- Another add slipped in between our read and insert. Loop to
                -- re-read the new max.
                if v_attempt = 2 then
                    raise exception 'could not allocate a queue position, please retry'
                        using errcode = '40001';
                end if;
        end;
    end loop;

    raise exception 'could not allocate a queue position, please retry'
        using errcode = '40001';
end;
$$;

-- Move a question to a new position and renumber the rest contiguously.
create or replace function public.qotd_move_question(
    p_guild_id    bigint,
    p_question_id bigint,
    p_new_position integer
)
returns void
language plpgsql
security invoker
as $$
declare
    v_target record;
begin
    if not public.can_manage_guild(p_guild_id) then
        raise exception 'not authorized for guild %', p_guild_id
            using errcode = '42501';
    end if;

    -- Lock the queue for this guild so concurrent reorders serialize.
    perform 1
       from public.qotd_questions
      where guild_id = p_guild_id
      order by id
      for update;

    select id, position
      into v_target
      from public.qotd_questions
     where id = p_question_id
       and guild_id = p_guild_id;

    if not found then
        raise exception 'question % not found in guild %', p_question_id, p_guild_id
            using errcode = 'P0002';
    end if;

    -- Detach, renumber everything after the old slot, then insert at the new slot.
    update public.qotd_questions
       set position = -1
     where id = p_question_id;

    update public.qotd_questions
       set position = position - 1
     where guild_id = p_guild_id
       and position > v_target.position;

    update public.qotd_questions
       set position = position + 1
     where guild_id = p_guild_id
       and position >= p_new_position;

    update public.qotd_questions
       set position = p_new_position
     where id = p_question_id;
end;
$$;

-- Compact positions after removals so ordering stays gap-free.
create or replace function public.qotd_renumber(p_guild_id bigint)
returns void
language plpgsql
security invoker
as $$
begin
    if not public.can_manage_guild(p_guild_id) then
        raise exception 'not authorized for guild %', p_guild_id
            using errcode = '42501';
    end if;

    with ordered as (
        select id,
               row_number() over (order by position, id) as rn
          from public.qotd_questions
         where guild_id = p_guild_id
    )
    update public.qotd_questions q
       set position = o.rn
      from ordered o
     where q.id = o.id
       and q.position <> o.rn;
end;
$$;

revoke all on function public.qotd_add_question(bigint, text) from public;
revoke all on function public.qotd_move_question(bigint, bigint, integer) from public;
revoke all on function public.qotd_renumber(bigint) from public;
grant execute on function public.qotd_add_question(bigint, text) to authenticated;
grant execute on function public.qotd_move_question(bigint, bigint, integer) to authenticated;
grant execute on function public.qotd_renumber(bigint) to authenticated;


-- ============================================================================
-- 05c  AI MEMORY RETENTION
-- ============================================================================
-- Called by the bot after each successful write, so storage stays bounded
-- without a scheduled job. Per-row expiry: each row dies on its own Nth
-- birthday. There is deliberately NO table wipe, and no count cap.
--
-- p_keep_days <= 0 means "keep forever" and is a no-op.
--
-- Depends on is_dev(), defined in section 04 ABOVE. Do not move this function
-- above that definition.

create or replace function public.ai_memory_prune(
    p_guild_id  bigint,
    p_keep_days integer default 30
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_deleted integer := 0;
    v_cutoff  timestamptz;
begin
    -- SERVICE ROLE OR DEV ONLY. Service role has auth.uid() IS NULL, so the
    -- is_dev() branch is what lets an operator run this by hand in the editor.
    if auth.uid() is not null and not public.is_dev() then
        raise exception 'not authorized' using errcode = '42501';
    end if;

    if p_keep_days is null or p_keep_days <= 0 then
        return 0;
    end if;

    v_cutoff := now() - make_interval(days => least(p_keep_days, 3650));

    delete from public.ai_memory
     where guild_id = p_guild_id
       and created_at < v_cutoff;

    get diagnostics v_deleted = row_count;
    return v_deleted;
end;
$$;

comment on function public.ai_memory_prune(bigint, integer) is
    'Deletes ai_memory rows older than p_keep_days for one guild. Per-row expiry: each row dies on its own Nth birthday, never a table wipe. p_keep_days <= 0 keeps forever. Called by the bot on write; also safe to run from the SQL editor as service_role.';

revoke all on function public.ai_memory_prune(bigint, integer) from public;
grant execute on function public.ai_memory_prune(bigint, integer) to authenticated;


-- ============================================================================
-- 06  REALTIME / REPLICATION
-- ============================================================================
-- The bot subscribes to these tables so config changes apply instantly.

do $$
begin
    -- REPLICA IDENTITY FULL so UPDATE/DELETE payloads carry the old row too.
    alter table public.guild_settings  replica identity full;
    alter table public.guild_replies   replica identity full;
    alter table public.qotd_questions  replica identity full;
    alter table public.guilds          replica identity full;
exception
    when others then
        raise notice 'replica identity step skipped: %', sqlerrm;
end $$;

-- Add tables to the supabase_realtime publication.
-- (Wrapped so re-running does not error if already added.)
do $$
begin
    begin
        alter publication supabase_realtime add table public.guild_settings;
    exception when duplicate_object then null;
    end;
    begin
        alter publication supabase_realtime add table public.guild_replies;
    exception when duplicate_object then null;
    end;
    begin
        alter publication supabase_realtime add table public.qotd_questions;
    exception when duplicate_object then null;
    end;
    begin
        alter publication supabase_realtime add table public.guilds;
    exception when duplicate_object then null;
    end;
end $$;

-- bot_events is intentionally NOT published: it is high-volume and the
-- dashboard can poll it. ai_interactions and ai_memory are NOT published
-- either - they contain user text, and broadcasting it to subscribers is
-- unnecessary.


-- ============================================================================
-- 07  LOGGING, AI STATS, AND THE ACTIVITY FEED
-- ============================================================================
-- Formerly migration_002_logging.sql. Folded in here so a fresh install needs
-- only this file. The tables (ai_interactions, bot_events, dev_users) and the
-- log_* columns are declared in sections 02 and 02b; what follows is the
-- admin-facing half: aggregate stats and the legacy rolling trim.


-- ---------------------------------------------------------------------------
-- ai_stats_for_guild(): aggregates with NO message text
-- ---------------------------------------------------------------------------
-- Guild admins get usage stats but cannot read what anyone said. Enforced by
-- the function's return type: there is simply no text column to leak.

create or replace function public.ai_stats_for_guild(
    p_guild_id bigint,
    p_days     integer default 30
)
returns table (
    total_interactions bigint,
    successful         bigint,
    failed             bigint,
    unique_users       bigint,
    avg_latency_ms     numeric,
    total_prompt_tokens bigint,
    total_completion_tokens bigint,
    busiest_day        date,
    busiest_day_count  bigint
)
language plpgsql
security definer
stable
set search_path = public
as $$
declare
    v_since timestamptz;
begin
    if not public.can_manage_guild(p_guild_id) then
        raise exception 'not authorized for guild %', p_guild_id
            using errcode = '42501';
    end if;

    v_since := now() - make_interval(days => greatest(1, least(p_days, 365)));

    return query
    with scoped as (
        select *
          from public.ai_interactions
         where guild_id = p_guild_id
           and created_at >= v_since
    ),
    by_day as (
        select date_trunc('day', created_at)::date as day, count(*) as cnt
          from scoped
         group by 1
         order by cnt desc
         limit 1
    )
    select
        (select count(*) from scoped)::bigint,
        (select count(*) from scoped where succeeded)::bigint,
        (select count(*) from scoped where not succeeded)::bigint,
        (select count(distinct user_id) from scoped)::bigint,
        (select round(avg(latency_ms), 1) from scoped where latency_ms is not null),
        (select coalesce(sum(prompt_tokens), 0) from scoped)::bigint,
        (select coalesce(sum(completion_tokens), 0) from scoped)::bigint,
        (select day from by_day),
        (select cnt from by_day)::bigint;
end;
$$;

comment on function public.ai_stats_for_guild(bigint, integer) is
    'Aggregate AI usage for guild admins. Returns counts only - never message text.';

revoke all on function public.ai_stats_for_guild(bigint, integer) from public;
grant execute on function public.ai_stats_for_guild(bigint, integer) to authenticated;


-- ---------------------------------------------------------------------------
-- ai_trim_channel(): rolling per-channel window over ai_interactions
-- ---------------------------------------------------------------------------
-- LEGACY. ai_interactions is analytics only now - memory lives in ai_memory
-- and is bounded by ai_memory_prune() above. Kept because the bot still calls
-- it defensively and because it is harmless. It is NOT the memory retention
-- mechanism; do not mistake it for one.
--
-- Keeps the newest p_keep summaries for a channel and deletes the rest.

create or replace function public.ai_trim_channel(
    p_guild_id   bigint,
    p_channel_id bigint,
    p_keep       integer default 50
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_deleted integer;
begin
    -- Service role or dev only. Regular users have no business trimming.
    if auth.role() <> 'service_role' and not public.is_dev() then
        raise exception 'not authorized to trim ai interactions'
            using errcode = '42501';
    end if;

    with doomed as (
        select id
          from public.ai_interactions
         where guild_id = p_guild_id
           and channel_id = p_channel_id
         order by created_at desc
        offset greatest(1, p_keep)
    )
    delete from public.ai_interactions a
     using doomed d
     where a.id = d.id;

    get diagnostics v_deleted = row_count;
    return v_deleted;
end;
$$;

comment on function public.ai_trim_channel(bigint, bigint, integer) is
    'LEGACY. Keeps only the newest p_keep ai_interactions summaries for a channel. Not the memory retention path - see ai_memory_prune().';

revoke all on function public.ai_trim_channel(bigint, bigint, integer) from public;
grant execute on function public.ai_trim_channel(bigint, bigint, integer) to authenticated, service_role;


-- ============================================================================
-- 07b  OPERATOR / ADMIN TABLES
-- ============================================================================
-- Everything here backs the owner-only admin dashboard. It is deliberately
-- RLS-on with NO policies: neither `anon` nor `authenticated` may touch these
-- tables. The bot (service_role) reads and writes them, and the admin-api Edge
-- Function (service_role, owner-gated) is the only browser-facing path.
--
-- WHY NOT is_dev() POLICIES LIKE ai_interactions HAS?
--   These tables hold other people's private messages and the audit trail.
--   Keeping them deny-all means a stolen dashboard JWT still cannot read them:
--   there is no policy that could ever match, so the rows are simply invisible.
--   The elevated read happens inside admin-api, behind the is_dev() check.


-- ---------------------------------------------------------------------------
-- guilds: live size snapshot, written by the bot
-- ---------------------------------------------------------------------------
-- online_count needs the privileged PRESENCE intent. When the bot cannot see
-- presence it stays NULL rather than reporting a made-up 0.

alter table public.guilds add column if not exists member_count     integer;
alter table public.guilds add column if not exists online_count     integer;
alter table public.guilds add column if not exists stats_updated_at timestamptz;

comment on column public.guilds.member_count is
    'Member count reported by Discord at the last stats snapshot. NULL = never sampled.';
comment on column public.guilds.online_count is
    'Members not offline at the last snapshot. NULL when the presence intent is unavailable.';


-- ---------------------------------------------------------------------------
-- bot_dms: incoming direct messages to the bot
-- ---------------------------------------------------------------------------
-- Storing DMs is NEW data collection and is disclosed in the Privacy Policy.
-- `content` may be empty when the message carried only attachments.

create table if not exists public.bot_dms (
    id              bigserial primary key,
    discord_user_id bigint not null,
    username        text,
    display_name    text,
    content         text   not null default '',
    attachments     jsonb  not null default '[]'::jsonb,
    message_id      bigint,
    created_at      timestamptz not null default now()
);

create index if not exists bot_dms_created_at_idx on public.bot_dms (created_at desc);
create index if not exists bot_dms_user_idx       on public.bot_dms (discord_user_id, created_at desc);

comment on table public.bot_dms is
    'Incoming DMs to the bot, for the owner admin console. Operator-only via admin-api; disclosed in the Privacy Policy.';


-- ---------------------------------------------------------------------------
-- bot_dm_outbox: operator replies, sent by the bot
-- ---------------------------------------------------------------------------
-- The browser never holds the bot token. It queues a row here; the bot polls,
-- sends, and records the outcome, so a send survives an admin tab closing.

create table if not exists public.bot_dm_outbox (
    id           bigserial primary key,
    recipient_id bigint not null,
    content      text   not null,
    status       text   not null default 'pending',
    error        text,
    requested_by uuid references auth.users(id) on delete set null,
    created_at   timestamptz not null default now(),
    sent_at      timestamptz,

    constraint bot_dm_outbox_status_valid check (status in ('pending','sent','failed'))
);

create index if not exists bot_dm_outbox_status_idx on public.bot_dm_outbox (status, created_at);

comment on table public.bot_dm_outbox is
    'Queued outbound DMs. The bot polls and sends them; status is pending -> sent | failed.';


-- ---------------------------------------------------------------------------
-- admin_audit: one row per operator write
-- ---------------------------------------------------------------------------

create table if not exists public.admin_audit (
    id          bigserial primary key,
    actor_id    uuid,
    actor_label text,
    action      text not null,
    target      text,
    detail      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);

create index if not exists admin_audit_created_at_idx on public.admin_audit (created_at desc);

comment on table public.admin_audit is
    'Append-only record of every admin-api write: who, what action, on which target.';


-- ---------------------------------------------------------------------------
-- admin_requests: work only the bot process can perform
-- ---------------------------------------------------------------------------
-- The Edge Function has no path to the Discord gateway, and rebuilding the
-- command tree in TypeScript would duplicate the Python tree. So such a request
-- is queued here and executed by the bot.

create table if not exists public.admin_requests (
    id           bigserial primary key,
    kind         text  not null,
    payload      jsonb not null default '{}'::jsonb,
    status       text  not null default 'pending',
    result       text,
    requested_by uuid,
    created_at   timestamptz not null default now(),
    processed_at timestamptz,

    constraint admin_requests_kind_valid   check (kind in ('resync_commands')),
    constraint admin_requests_status_valid check (status in ('pending','done','failed'))
);

create index if not exists admin_requests_status_idx on public.admin_requests (status, created_at);

comment on table public.admin_requests is
    'Requests the Edge Function cannot perform itself. The bot polls and executes, then records the result.';


-- ---------------------------------------------------------------------------
-- RLS: deny-all. No policies are created on purpose.
-- ---------------------------------------------------------------------------

alter table public.bot_dms        enable row level security;
alter table public.bot_dm_outbox  enable row level security;
alter table public.admin_audit    enable row level security;
alter table public.admin_requests enable row level security;

alter table public.bot_dms        force row level security;
alter table public.bot_dm_outbox  force row level security;
alter table public.admin_audit    force row level security;
alter table public.admin_requests force row level security;


-- ============================================================================
-- 07c  ACTIVITY TRACKING + MONTHLY REPORTS
-- ============================================================================
-- The bot keeps per-guild activity counters IN MEMORY and flushes them every
-- few minutes with one RPC per guild (activity_flush). Nothing here is written
-- per message, which is what makes this affordable on a 256 MB host.
--
-- Buckets are LOCAL days/hours in the guild's report_timezone, not UTC, so
-- "most active day" and "most active time" mean what an admin expects.
--
-- No message text is stored anywhere in this section: counts only. Per-user
-- rows hold a display-name snapshot so a report still reads correctly after a
-- member leaves, and the name is refreshed at report time from Discord.
--
-- RLS is deny-all (no policies) like the operator tables. The dashboard is not
-- wired to these tables yet; when it is, add SELECT policies via
-- can_manage_guild() rather than opening them up.

-- ---------------------------------------------------------------------------
-- guild_daily_stats: one row per guild per local day
-- ---------------------------------------------------------------------------
create table if not exists public.guild_daily_stats (
    guild_id           bigint not null references public.guilds(id) on delete cascade,
    day                date   not null,
    member_count       integer,                 -- snapshot at the last flush of the day
    online_count       integer,                 -- NULL when the presence intent is off
    boost_count        integer,                 -- current boost total at the last flush
    joins              integer not null default 0,
    leaves             integer not null default 0,
    boosts_gained      integer not null default 0,
    boosts_lost        integer not null default 0,
    messages           integer not null default 0,
    voice_seconds      bigint  not null default 0,
    voice_joins        integer not null default 0,
    bot_online_seconds integer not null default 0, -- how much of the day was actually observed
    updated_at         timestamptz not null default now(),
    primary key (guild_id, day)
);

comment on table public.guild_daily_stats is
    'Per-guild daily activity totals. Written by activity_flush(); no message text.';
comment on column public.guild_daily_stats.bot_online_seconds is
    'Seconds the bot was actually connected and flushing this day. Used to disclose gaps in a report.';

-- ---------------------------------------------------------------------------
-- guild_channel_activity: per-channel, per-day totals
-- ---------------------------------------------------------------------------
create table if not exists public.guild_channel_activity (
    guild_id      bigint not null references public.guilds(id) on delete cascade,
    day           date   not null,
    channel_id    bigint not null,
    channel_name  text,                          -- snapshot, for reports after deletion
    messages      integer not null default 0,
    voice_seconds bigint  not null default 0,
    voice_joins   integer not null default 0,
    updated_at    timestamptz not null default now(),
    primary key (guild_id, day, channel_id)
);

comment on table public.guild_channel_activity is
    'Per-channel daily message/voice totals. channel_name is a display snapshot, not a live join.';

-- ---------------------------------------------------------------------------
-- guild_hourly_activity: per local hour, per day
-- ---------------------------------------------------------------------------
create table if not exists public.guild_hourly_activity (
    guild_id      bigint not null references public.guilds(id) on delete cascade,
    day           date   not null,
    hour          smallint not null,             -- 0..23 local time
    messages      integer not null default 0,
    voice_seconds bigint  not null default 0,
    updated_at    timestamptz not null default now(),
    primary key (guild_id, day, hour),
    constraint guild_hourly_activity_hour_range check (hour >= 0 and hour <= 23)
);

comment on table public.guild_hourly_activity is
    'Per-local-hour activity buckets, used for "most active time" in the monthly report.';

-- ---------------------------------------------------------------------------
-- guild_member_activity: per-user, per-day totals
-- ---------------------------------------------------------------------------
create table if not exists public.guild_member_activity (
    guild_id      bigint not null references public.guilds(id) on delete cascade,
    day           date   not null,
    user_id       bigint not null,
    user_name     text,                          -- display-name snapshot
    messages      integer not null default 0,
    voice_seconds bigint  not null default 0,
    voice_joins   integer not null default 0,
    updated_at    timestamptz not null default now(),
    primary key (guild_id, day, user_id)
);

comment on table public.guild_member_activity is
    'Per-member daily activity counts. No content: messages and voice seconds only. Names are snapshots.';

-- ---------------------------------------------------------------------------
-- guild_boost_events: who boosted, and when
-- ---------------------------------------------------------------------------
create table if not exists public.guild_boost_events (
    id          bigserial primary key,
    guild_id    bigint not null references public.guilds(id) on delete cascade,
    occurred_at timestamptz not null default now(),
    user_id     bigint,
    user_name   text,
    kind        text not null,                   -- start | stop
    source      text not null default 'gateway',
    constraint guild_boost_events_kind_valid   check (kind in ('start','stop')),
    constraint guild_boost_events_source_valid check (source in ('gateway','manual'))
);

create index if not exists guild_boost_events_guild_idx
    on public.guild_boost_events (guild_id, occurred_at desc);

comment on table public.guild_boost_events is
    'One row per boost gained/lost, with the member who did it when Discord reports it.';

-- ---------------------------------------------------------------------------
-- guild_moderation_events: audit log + Dyno warnings
-- ---------------------------------------------------------------------------
-- SOURCE OF TRUTH PER ACTION:
--   ban / unban / kick / timeout -> Discord audit log (source='audit')
--   warn                         -> Dyno mod-log embed  (source='dyno_embed')
--   anything entered by hand     -> source='manual'
--
-- A Dyno ban/kick embed is deliberately IGNORED: the audit log already records
-- the action, so parsing both would double-count. The cost of that choice is
-- that actions performed THROUGH Dyno name Dyno as the actor, not the human
-- moderator; `via` records which case it was so a report can be honest about it.
--
-- external_id is the audit entry id or the Discord message id. The unique index
-- is what makes re-polling and restarts idempotent.
create table if not exists public.guild_moderation_events (
    id             bigserial primary key,
    guild_id       bigint not null references public.guilds(id) on delete cascade,
    occurred_at    timestamptz not null default now(),
    kind           text not null,                -- warn|timeout|kick|ban|unban|report|other
    target_id      bigint,
    target_name    text,
    moderator_id   bigint,
    moderator_name text,
    reason         text,
    source         text not null,                -- audit|dyno_embed|manual
    via            text,                         -- native|dyno (who actually issued it)
    external_id    text,                         -- audit entry id or Discord message id
    case_number    integer,                      -- Dyno case number, informational only
    raw_embed      jsonb,                        -- the original Dyno embed, for re-parsing
    needs_review   boolean not null default false,
    created_at     timestamptz not null default now(),
    constraint guild_moderation_events_kind_valid
        check (kind in ('warn','timeout','kick','ban','unban','report','other')),
    constraint guild_moderation_events_source_valid
        check (source in ('audit','dyno_embed','manual')),
    constraint guild_moderation_events_via_valid
        check (via is null or via in ('native','dyno')),
    constraint guild_moderation_events_reason_length
        check (reason is null or length(reason) <= 1000)
);

-- NULL external_id is allowed to repeat (manual rows); Postgres treats NULLs as
-- distinct in a unique index, which is exactly what we want.
create unique index if not exists guild_moderation_events_dedupe_idx
    on public.guild_moderation_events (guild_id, source, external_id);

create index if not exists guild_moderation_events_guild_idx
    on public.guild_moderation_events (guild_id, occurred_at desc);

create index if not exists guild_moderation_events_kind_idx
    on public.guild_moderation_events (guild_id, kind, occurred_at desc);

comment on table public.guild_moderation_events is
    'Moderation actions for reports. Audit log for ban/kick/unban/timeout, Dyno embed for warnings.';

-- ---------------------------------------------------------------------------
-- guild_moderation_cursors: how far the audit-log poller has read
-- ---------------------------------------------------------------------------
create table if not exists public.guild_moderation_cursors (
    guild_id      bigint primary key references public.guilds(id) on delete cascade,
    last_audit_id bigint,
    updated_at    timestamptz not null default now()
);

comment on table public.guild_moderation_cursors is
    'Per-guild audit-log cursor. Advanced only after the corresponding events are written.';

-- ---------------------------------------------------------------------------
-- guild_events: scheduled events mirrored from Discord
-- ---------------------------------------------------------------------------
create table if not exists public.guild_events (
    guild_id         bigint not null references public.guilds(id) on delete cascade,
    event_id         bigint not null,
    name             text,
    description      text,
    start_time       timestamptz,
    end_time         timestamptz,
    status           text,                       -- scheduled|active|completed|canceled
    user_count       integer,
    subscriber_count integer,
    channel_id       bigint,
    updated_at       timestamptz not null default now(),
    primary key (guild_id, event_id),
    constraint guild_events_status_valid
        check (status is null or status in ('scheduled','active','completed','canceled'))
);

create index if not exists guild_events_start_idx
    on public.guild_events (guild_id, start_time);

-- Discord stops returning an event once it ends or is deleted. Deleting the
-- row would erase exactly the history this table exists to keep, so the sync
-- marks it instead.
alter table public.guild_events
    add column if not exists removed_at timestamptz;

comment on column public.guild_events.removed_at is
    'Set when Discord no longer returns the event. The row is kept so past events still count.';

comment on table public.guild_events is
    'Discord scheduled events, mirrored so the monthly report can count them after they expire.';

-- ---------------------------------------------------------------------------
-- guild_reports: the generated monthly report
-- ---------------------------------------------------------------------------
-- auto_data is frozen JSON from the moment of generation, so a published report
-- keeps its numbers even after retention prunes the source rows.
-- narrative holds the human-written sections and is preserved across regenerates.
create table if not exists public.guild_reports (
    id                   bigserial primary key,
    guild_id             bigint not null references public.guilds(id) on delete cascade,
    period_start         date not null,
    period_end           date not null,
    status               text not null default 'draft',
    prepared_by          text,
    auto_data            jsonb not null default '{}'::jsonb,
    narrative            jsonb not null default '{}'::jsonb,
    markdown             text,
    published_at         timestamptz,
    published_channel_id bigint,
    published_message_id bigint,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    unique (guild_id, period_start),
    constraint guild_reports_status_valid check (status in ('draft','published')),
    constraint guild_reports_period_order check (period_end >= period_start)
);

create index if not exists guild_reports_guild_idx
    on public.guild_reports (guild_id, period_start desc);

drop trigger if exists guild_reports_touch_updated_at on public.guild_reports;
create trigger guild_reports_touch_updated_at
    before update on public.guild_reports
    for each row execute function public.touch_updated_at();

comment on table public.guild_reports is
    'Generated monthly reports. auto_data is a frozen snapshot; narrative is editable and survives regeneration.';

-- ---------------------------------------------------------------------------
-- guild_ticket_logs: summaries produced by the !log command
-- ---------------------------------------------------------------------------
-- The JSON file posted to the ticket channel is also stored here, so the
-- monthly report can count tickets handled and the dashboard can query past
-- summaries. `summary` holds exactly the four keys the model is asked for:
-- {"people_involved": [...], "when": "...", "why": "...", "ticket_type": "..."}.
--
-- Every ticket channel is named `ticket-0001`, and that number restarts per
-- ticket type, so the name is neither a type nor a unique ticket id. The type
-- is inferred from the conversation and copied into `ticket_type` for cheap
-- grouping; `channel_id` is what actually distinguishes recurring names.
create table if not exists public.guild_ticket_logs (
    id                bigserial primary key,
    guild_id          bigint not null references public.guilds(id) on delete cascade,
    channel_id        bigint not null,
    channel_name      text,
    ticket_type       text,
    generated_by      bigint,
    generated_by_name text,
    message_count     integer not null default 0,
    summary           jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now()
);

-- `create table if not exists` is a no-op on an existing database, so a column
-- added after the first deploy needs its own statement.
alter table public.guild_ticket_logs
    add column if not exists ticket_type text;

create index if not exists guild_ticket_logs_guild_idx
    on public.guild_ticket_logs (guild_id, created_at desc);

create index if not exists guild_ticket_logs_channel_idx
    on public.guild_ticket_logs (guild_id, channel_id, created_at desc);

create index if not exists guild_ticket_logs_type_idx
    on public.guild_ticket_logs (guild_id, ticket_type, created_at desc);

comment on table public.guild_ticket_logs is
    'AI summaries of ticket channels exported with !log. summary is the exact four-key JSON sent to Discord; ticket_type is copied out for report grouping.';

-- ---------------------------------------------------------------------------
-- RLS: deny-all, same reasoning as the operator tables.
-- ---------------------------------------------------------------------------
alter table public.guild_daily_stats         enable row level security;
alter table public.guild_channel_activity    enable row level security;
alter table public.guild_hourly_activity     enable row level security;
alter table public.guild_member_activity     enable row level security;
alter table public.guild_boost_events        enable row level security;
alter table public.guild_moderation_events   enable row level security;
alter table public.guild_moderation_cursors  enable row level security;
alter table public.guild_events              enable row level security;
alter table public.guild_reports             enable row level security;
alter table public.guild_ticket_logs         enable row level security;

alter table public.guild_daily_stats         force row level security;
alter table public.guild_channel_activity    force row level security;
alter table public.guild_hourly_activity     force row level security;
alter table public.guild_member_activity     force row level security;
alter table public.guild_boost_events        force row level security;
alter table public.guild_moderation_events   force row level security;
alter table public.guild_moderation_cursors  force row level security;
alter table public.guild_events              force row level security;
alter table public.guild_reports             force row level security;
alter table public.guild_ticket_logs         force row level security;


-- ---------------------------------------------------------------------------
-- activity_flush(): one atomic write per guild per flush
-- ---------------------------------------------------------------------------
-- The bot accumulates counters in memory and calls this every few minutes.
-- Doing it in one function means one round trip per guild instead of one per
-- channel/user/hour, and `on conflict do update set x = x + excluded.x` is
-- atomic, so a retry can never double-count a flush that already landed.
--
-- Payload shape:
--   {
--     "day": "2026-11-10",
--     "member_count": 123, "online_count": 45, "boost_count": 7,
--     "daily": {"joins":0,"leaves":0,"boosts_gained":0,"boosts_lost":0,
--               "messages":0,"voice_seconds":0,"voice_joins":0,
--               "bot_online_seconds":300},
--     "channels": [{"channel_id":1,"channel_name":"#general",
--                   "messages":10,"voice_seconds":0,"voice_joins":0}],
--     "hours":    [{"hour":13,"messages":10,"voice_seconds":0}],
--     "members":  [{"user_id":1,"user_name":"x",
--                   "messages":10,"voice_seconds":0,"voice_joins":0}]
--   }
create or replace function public.activity_flush(
    p_guild_id bigint,
    p_payload  jsonb
)
returns void
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_day   date  := coalesce((p_payload->>'day')::date, current_date);
    v_daily jsonb := coalesce(p_payload->'daily', '{}'::jsonb);
    v_item  jsonb;
begin
    insert into public.guild_daily_stats as d (
        guild_id, day, member_count, online_count, boost_count,
        joins, leaves, boosts_gained, boosts_lost,
        messages, voice_seconds, voice_joins, bot_online_seconds
    ) values (
        p_guild_id, v_day,
        (p_payload->>'member_count')::integer,
        (p_payload->>'online_count')::integer,
        (p_payload->>'boost_count')::integer,
        coalesce((v_daily->>'joins')::integer, 0),
        coalesce((v_daily->>'leaves')::integer, 0),
        coalesce((v_daily->>'boosts_gained')::integer, 0),
        coalesce((v_daily->>'boosts_lost')::integer, 0),
        coalesce((v_daily->>'messages')::integer, 0),
        coalesce((v_daily->>'voice_seconds')::bigint, 0),
        coalesce((v_daily->>'voice_joins')::integer, 0),
        coalesce((v_daily->>'bot_online_seconds')::integer, 0)
    )
    on conflict (guild_id, day) do update set
        member_count       = coalesce(excluded.member_count, d.member_count),
        online_count       = coalesce(excluded.online_count, d.online_count),
        boost_count        = coalesce(excluded.boost_count,  d.boost_count),
        joins              = d.joins              + excluded.joins,
        leaves             = d.leaves             + excluded.leaves,
        boosts_gained      = d.boosts_gained      + excluded.boosts_gained,
        boosts_lost        = d.boosts_lost        + excluded.boosts_lost,
        messages           = d.messages           + excluded.messages,
        voice_seconds      = d.voice_seconds      + excluded.voice_seconds,
        voice_joins        = d.voice_joins        + excluded.voice_joins,
        bot_online_seconds = d.bot_online_seconds + excluded.bot_online_seconds,
        updated_at         = now();

    for v_item in
        select value from jsonb_array_elements(coalesce(p_payload->'channels', '[]'::jsonb))
    loop
        insert into public.guild_channel_activity as c (
            guild_id, day, channel_id, channel_name, messages, voice_seconds, voice_joins
        ) values (
            p_guild_id, v_day,
            (v_item->>'channel_id')::bigint,
            nullif(v_item->>'channel_name', ''),
            coalesce((v_item->>'messages')::integer, 0),
            coalesce((v_item->>'voice_seconds')::bigint, 0),
            coalesce((v_item->>'voice_joins')::integer, 0)
        )
        on conflict (guild_id, day, channel_id) do update set
            channel_name  = coalesce(excluded.channel_name, c.channel_name),
            messages      = c.messages      + excluded.messages,
            voice_seconds = c.voice_seconds + excluded.voice_seconds,
            voice_joins   = c.voice_joins   + excluded.voice_joins,
            updated_at    = now();
    end loop;

    for v_item in
        select value from jsonb_array_elements(coalesce(p_payload->'hours', '[]'::jsonb))
    loop
        insert into public.guild_hourly_activity as h (
            guild_id, day, hour, messages, voice_seconds
        ) values (
            p_guild_id, v_day,
            (v_item->>'hour')::smallint,
            coalesce((v_item->>'messages')::integer, 0),
            coalesce((v_item->>'voice_seconds')::bigint, 0)
        )
        on conflict (guild_id, day, hour) do update set
            messages      = h.messages      + excluded.messages,
            voice_seconds = h.voice_seconds + excluded.voice_seconds,
            updated_at    = now();
    end loop;

    for v_item in
        select value from jsonb_array_elements(coalesce(p_payload->'members', '[]'::jsonb))
    loop
        insert into public.guild_member_activity as m (
            guild_id, day, user_id, user_name, messages, voice_seconds, voice_joins
        ) values (
            p_guild_id, v_day,
            (v_item->>'user_id')::bigint,
            nullif(v_item->>'user_name', ''),
            coalesce((v_item->>'messages')::integer, 0),
            coalesce((v_item->>'voice_seconds')::bigint, 0),
            coalesce((v_item->>'voice_joins')::integer, 0)
        )
        on conflict (guild_id, day, user_id) do update set
            user_name     = coalesce(excluded.user_name, m.user_name),
            messages      = m.messages      + excluded.messages,
            voice_seconds = m.voice_seconds + excluded.voice_seconds,
            voice_joins   = m.voice_joins   + excluded.voice_joins,
            updated_at    = now();
    end loop;
end;
$$;

comment on function public.activity_flush(bigint, jsonb) is
    'Atomically merges one in-memory activity snapshot. One call per guild per flush.';

revoke all on function public.activity_flush(bigint, jsonb) from public;
grant execute on function public.activity_flush(bigint, jsonb) to service_role;


-- ---------------------------------------------------------------------------
-- activity_prune(): retention for the high-cardinality tables
-- ---------------------------------------------------------------------------
-- guild_daily_stats, reports, boosts and moderation events are kept. Only the
-- per-channel/per-hour/per-member detail is aged out, because that is the part
-- that grows with traffic. A published report keeps its own frozen copy.
create or replace function public.activity_prune(
    p_days integer default 400
)
returns integer
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_days   integer := greatest(30, least(coalesce(p_days, 400), 3650));
    v_cutoff date    := current_date - v_days;
    v_total  integer := 0;
    v_count  integer;
begin
    delete from public.guild_member_activity  where day < v_cutoff;
    get diagnostics v_count = row_count;
    v_total := v_total + v_count;

    delete from public.guild_channel_activity where day < v_cutoff;
    get diagnostics v_count = row_count;
    v_total := v_total + v_count;

    delete from public.guild_hourly_activity  where day < v_cutoff;
    get diagnostics v_count = row_count;
    v_total := v_total + v_count;

    return v_total;
end;
$$;

comment on function public.activity_prune(integer) is
    'Deletes per-member/channel/hour activity older than p_days (clamped 30..3650). Daily totals and reports are kept.';

revoke all on function public.activity_prune(integer) from public;
grant execute on function public.activity_prune(integer) to service_role;


-- ---------------------------------------------------------------------------
-- report_summary(): every aggregate the markdown template needs, in one call
-- ---------------------------------------------------------------------------
-- Doing this in Postgres keeps a 256 MB bot host from pulling a month of
-- per-member rows over the wire just to sum them.
--
-- p_start_at / p_end_at are the period boundaries in the GUILD's timezone,
-- converted to UTC by the bot. They matter for timestamped rows (moderation,
-- events); the daily/channel/member tables are already bucketed by local day.
create or replace function public.report_summary(
    p_guild_id bigint,
    p_start    date,
    p_end      date,
    p_start_at timestamptz default null,
    p_end_at   timestamptz default null
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_start_at timestamptz := coalesce(p_start_at, p_start::timestamptz);
    v_end_at   timestamptz := coalesce(p_end_at, (p_end + 1)::timestamptz);
begin
    return (
        with daily as (
            select *
              from public.guild_daily_stats
             where guild_id = p_guild_id
               and day between p_start and p_end
        ),
        totals as (
            select
                coalesce(sum(messages), 0)::bigint      as messages,
                coalesce(sum(voice_seconds), 0)::bigint as voice_seconds,
                coalesce(sum(voice_joins), 0)::bigint   as voice_joins,
                coalesce(sum(joins), 0)::bigint         as joins,
                coalesce(sum(leaves), 0)::bigint        as leaves,
                coalesce(sum(boosts_gained), 0)::bigint as boosts_gained,
                coalesce(sum(boosts_lost), 0)::bigint   as boosts_lost,
                coalesce(sum(bot_online_seconds), 0)::bigint
                                                        as bot_online_seconds,
                count(*)::bigint                        as days_tracked
              from daily
        ),
        bounds as (
            select
                (select member_count from daily where member_count is not null
                  order by day asc  limit 1) as member_start,
                (select member_count from daily where member_count is not null
                  order by day desc limit 1) as member_end,
                (select boost_count  from daily where boost_count  is not null
                  order by day desc limit 1) as boost_count
        ),
        top_day as (
            select day, messages
              from daily
             order by messages desc, day asc
             limit 1
        ),
        top_hour as (
            select hour, sum(messages)::bigint as messages
              from public.guild_hourly_activity
             where guild_id = p_guild_id
               and day between p_start and p_end
             group by hour
             order by sum(messages) desc, hour asc
             limit 1
        ),
        channel_rows as (
            select channel_id,
                   max(channel_name)          as channel_name,
                   sum(messages)::bigint      as messages,
                   sum(voice_seconds)::bigint as voice_seconds,
                   sum(voice_joins)::bigint   as voice_joins
              from public.guild_channel_activity
             where guild_id = p_guild_id
               and day between p_start and p_end
             group by channel_id
        ),
        top_channels as (
            select coalesce(
                       jsonb_agg(to_jsonb(x) order by x.messages desc, x.channel_id),
                       '[]'::jsonb) as data
              from (select * from channel_rows
                     order by messages desc, channel_id
                     limit 5) x
        ),
        top_voice_channels as (
            select coalesce(
                       jsonb_agg(to_jsonb(x) order by x.voice_seconds desc, x.channel_id),
                       '[]'::jsonb) as data
              from (select * from channel_rows
                     where voice_seconds > 0
                     order by voice_seconds desc, channel_id
                     limit 5) x
        ),
        member_rows as (
            select user_id,
                   max(user_name)             as user_name,
                   sum(messages)::bigint      as messages,
                   sum(voice_seconds)::bigint as voice_seconds
              from public.guild_member_activity
             where guild_id = p_guild_id
               and day between p_start and p_end
             group by user_id
        ),
        top_message_users as (
            select coalesce(
                       jsonb_agg(to_jsonb(x) order by x.messages desc, x.user_id),
                       '[]'::jsonb) as data
              from (select * from member_rows
                     where messages > 0
                     order by messages desc, user_id
                     limit 10) x
        ),
        top_voice_users as (
            select coalesce(
                       jsonb_agg(to_jsonb(x) order by x.voice_seconds desc, x.user_id),
                       '[]'::jsonb) as data
              from (select * from member_rows
                     where voice_seconds > 0
                     order by voice_seconds desc, user_id
                     limit 10) x
        ),
        ticket_rows as (
            -- One ticket channel can be exported more than once (for example,
            -- after more messages arrive). Count the channel once, using its
            -- latest summary, so the total and the per-type breakdown agree.
            select distinct on (channel_id)
                   channel_id,
                   coalesce(nullif(ticket_type, ''), 'Other') as ticket_type
              from public.guild_ticket_logs
             where guild_id = p_guild_id
               and created_at >= v_start_at
               and created_at <  v_end_at
             order by channel_id, created_at desc
        ),
        ticket_totals as (
            select
                coalesce(sum(n), 0)::bigint                 as total,
                coalesce(jsonb_object_agg(ticket_type, n),
                         '{}'::jsonb)                       as by_type
              from (
                    select ticket_type, count(*)::bigint as n
                      from ticket_rows
                     group by ticket_type
              ) x
        ),
        moderation as (
            select
                count(*) filter (where kind = 'warn')::bigint    as warns,
                count(*) filter (where kind = 'timeout')::bigint as timeouts,
                count(*) filter (where kind = 'kick')::bigint    as kicks,
                count(*) filter (where kind = 'ban')::bigint     as bans,
                count(*) filter (where kind = 'unban')::bigint   as unbans,
                -- Nothing writes kind='report': this bot has no "report a
                -- user" command, so the old filter was always zero. The
                -- meaningful count is the tickets the AI classified as a
                -- user report, which !log now produces.
                coalesce(
                    (select (by_type->>'Reporting a user')::bigint
                       from ticket_totals),
                    0)::bigint                                   as reports,
                count(*) filter (where via = 'dyno')::bigint     as via_dyno,
                count(*) filter (where via = 'native')::bigint   as via_native,
                (select total   from ticket_totals)             as tickets_logged,
                (select by_type from ticket_totals)             as tickets_by_type
              from public.guild_moderation_events
             where guild_id = p_guild_id
               and occurred_at >= v_start_at
               and occurred_at <  v_end_at
        ),
        staff_actions as (
            select coalesce(
                       jsonb_agg(to_jsonb(x) order by x.actions desc, x.moderator_id),
                       '[]'::jsonb) as data
              from (
                    select moderator_id,
                           max(moderator_name) as moderator_name,
                           count(*)::bigint    as actions
                      from public.guild_moderation_events
                     where guild_id = p_guild_id
                       and occurred_at >= v_start_at
                       and occurred_at <  v_end_at
                       and kind in ('warn','timeout','kick','ban')
                     group by moderator_id, moderator_name
                     order by count(*) desc, moderator_id
                     limit 10
              ) x
        ),
        event_totals as (
            select
                count(*) filter (
                    where start_time >= v_start_at and start_time < v_end_at
                )::bigint as hosted,
                coalesce(sum(user_count) filter (
                    where start_time >= v_start_at and start_time < v_end_at
                ), 0)::bigint as participation,
                count(*) filter (
                    where start_time >= v_start_at and start_time < v_end_at
                      and (status = 'completed'
                           or (end_time is not null and end_time < now()))
                )::bigint as successful
              from public.guild_events
             where guild_id = p_guild_id
        ),
        upcoming as (
            select coalesce(
                       jsonb_agg(to_jsonb(x) order by x.start_time),
                       '[]'::jsonb) as data
              from (
                    select event_id, name, start_time, status
                      from public.guild_events
                     where guild_id = p_guild_id
                       and removed_at is null
                       and start_time > now()
                     order by start_time
                     limit 5
              ) x
        )
        select jsonb_build_object(
            'guild_id', p_guild_id,
            'period_start', p_start,
            'period_end', p_end,
            'member_start', (select member_start from bounds),
            'member_end',   (select member_end   from bounds),
            'member_growth',
                case
                    when (select member_end from bounds) is not null
                     and (select member_start from bounds) is not null
                    then (select member_end from bounds)
                       - (select member_start from bounds)
                end,
            'boost_count',      (select boost_count      from bounds),
            'total_messages',   (select messages          from totals),
            'total_voice_seconds', (select voice_seconds  from totals),
            'voice_joins',      (select voice_joins       from totals),
            'total_joins',      (select joins             from totals),
            'total_leaves',     (select leaves            from totals),
            'boosts_gained',    (select boosts_gained     from totals),
            'boosts_lost',      (select boosts_lost       from totals),
            'bot_online_seconds', (select bot_online_seconds from totals),
            'days_tracked',     (select days_tracked      from totals),
            'most_active_day',
                (select jsonb_build_object('day', day, 'messages', messages)
                   from top_day),
            'most_active_hour',
                (select jsonb_build_object('hour', hour, 'messages', messages)
                   from top_hour),
            'top_channels',       (select data from top_channels),
            'top_voice_channels', (select data from top_voice_channels),
            'top_message_users',  (select data from top_message_users),
            'top_voice_users',    (select data from top_voice_users),
            'moderation',         (select to_jsonb(moderation) from moderation),
            'staff_actions',      (select data from staff_actions),
            'events', jsonb_build_object(
                'hosted',        (select hosted        from event_totals),
                'participation', (select participation from event_totals),
                'successful',    (select successful    from event_totals),
                'upcoming',      (select data          from upcoming)
            )
        )
    );
end;
$$;

comment on function public.report_summary(bigint, date, date, timestamptz, timestamptz) is
    'One-call aggregate for the monthly report template. Counts only, no message text.';

revoke all on function public.report_summary(bigint, date, date, timestamptz, timestamptz) from public;
grant execute on function public.report_summary(bigint, date, date, timestamptz, timestamptz) to service_role;


-- ============================================================================
-- 08  VERIFICATION QUERIES
-- ============================================================================
-- Run these after the script to confirm everything landed.

-- 08.1 Tables exist (expect 26 rows)
-- select tablename
--   from pg_tables
--  where schemaname = 'public'
--    and tablename in ('guilds','guild_settings','user_guilds',
--                      'reply_actions','guild_replies','qotd_questions',
--                      'ai_memory','ai_interactions','bot_events','dev_users',
--                      'user_preferences','bot_dms','bot_dm_outbox',
--                      'admin_audit','admin_requests',
--                      'guild_daily_stats','guild_channel_activity',
--                      'guild_hourly_activity','guild_member_activity',
--                      'guild_boost_events','guild_moderation_events',
--                      'guild_moderation_cursors','guild_events','guild_reports',
--                      'guild_ticket_logs')
--  order by tablename;

-- 08.2 RLS enabled on all 26 (expect rls_enabled = true everywhere)
-- select relname as table_name, relrowsecurity as rls_enabled, relforcerowsecurity as rls_forced
--   from pg_class
--  where relname in ('guilds','guild_settings','user_guilds',
--                    'reply_actions','guild_replies','qotd_questions',
--                    'ai_memory','ai_interactions','bot_events','dev_users',
--                    'user_preferences','bot_dms','bot_dm_outbox',
--                    'admin_audit','admin_requests',
--                    'guild_daily_stats','guild_channel_activity',
--                    'guild_hourly_activity','guild_member_activity',
--                    'guild_boost_events','guild_moderation_events',
--                    'guild_moderation_cursors','guild_events','guild_reports',
--                    'guild_ticket_logs');

-- 08.3 Policy count per table (expect a sensible number for the user-facing
--      tables and ZERO for the operator tables, which are deny-all by design).
-- select tablename, count(*) as policy_count
--   from pg_policies
--  where schemaname = 'public'
--  group by tablename
--  order by tablename;

-- 08.4 Seed catalog loaded (expect 13 rows - run supabase/seed_replies.sql first)
-- select key, label, array_length(default_templates, 1) as template_count
--   from public.reply_actions
--  order by sort_order;

-- 08.5 Helper functions exist (expect 6 rows)
-- select proname, prosecdef as is_security_definer
--   from pg_proc
--  where proname in ('can_manage_guild','is_dev','qotd_add_question',
--                    'qotd_move_question','qotd_renumber','ai_memory_prune');

-- 08.6 The dependency chain actually works (expect 0, not an error).
--      This is the real test: before is_dev() was folded into this file,
--      calling this raised because is_dev() did not exist.
-- select public.ai_memory_prune(0, 30);

-- 08.7 Make yourself a dev (replacing the uuid). Grants elevated dashboard reads.
-- insert into public.dev_users (user_id, note)
-- select id, 'owner' from auth.users where email = 'your@email.com'
-- on conflict (user_id) do nothing;

-- 08.8 Every column the BOT WRITES must exist (expect zero rows).
--      prefix_enabled was missing for the whole life of the project: the bot
--      wrote it, no schema file created it, and the failure looked like "the
--      toggle does nothing". This compares the live table against the columns
--      the bot actually uses, so a future rename drops out here instead of in
--      production. Update the list when a new column is added.
-- select missing.column_name
--   from (values
--           ('prefix'),('prefix_enabled'),('log_channel_id'),('log_enabled'),
--           ('log_categories'),('ai_enabled'),('ai_model'),('ai_system_prompt'),
--           ('ai_max_tokens'),('ai_temperature'),('ai_context_messages'),
--           ('ai_memory_enabled'),('ai_memory_days'),
--           ('ai_memory_ignored_channels'),('ai_user_cooldown_seconds'),
--           ('ai_all_channels'),('ai_channel_ids'),('qotd_enabled'),
--           ('qotd_channel_id'),('qotd_role_id'),('qotd_time'),('qotd_timezone'),
--           ('qotd_days'),('qotd_empty_queue_notice'),('qotd_last_fired_at'),
--           ('report_enabled'),('report_channel_id'),('report_day'),
--           ('report_time'),('report_timezone'),
--           ('report_last_generated_period'),('moderation_log_channel_id'),
--           ('dyno_user_id'),('staff_role_ids')
--        ) as missing(column_name)
--  where not exists (
--        select 1 from information_schema.columns
--         where table_schema = 'public'
--           and table_name = 'guild_settings'
--           and columns.column_name = missing.column_name
--  );

-- 08.9 qotd_empty_queue_notice should stay false unless an admin asked for it
-- select guild_id, qotd_empty_queue_notice from public.guild_settings
--  where qotd_empty_queue_notice;

-- 08.10 user_preferences has owner-only policies (expect 4 rows).
--       The dashboard writes the chosen theme here when signed in.
-- select policyname, cmd from pg_policies
--  where schemaname = 'public' and tablename = 'user_preferences'
--  order by policyname;

-- 08.11 The operator tables are deny-all (expect ZERO rows for each).
--       A policy appearing here means scope crept in and a logged-in user
--       could read DMs or the audit trail directly.
-- select tablename, count(*) as policy_count
--   from pg_policies
--  where schemaname = 'public'
--    and tablename in ('bot_dms','bot_dm_outbox','admin_audit','admin_requests')
--  group by tablename
--  order by tablename;

-- 08.12 Make your ADMIN account a dev (replacing the email). With Google or
--       Discord sign-in alike, the account must exist in auth.users first, so
--       sign in to admin_web once, then run this.
-- insert into public.dev_users (user_id, note)
-- select id, 'admin' from auth.users where email = 'you@example.com'
-- on conflict (user_id) do nothing;

-- 08.13 Every 64-bit column in public (expect the list below). The admin
--       console's read-only table browser casts each of these to text, because
--       PostgREST would otherwise send a Discord snowflake as a JSON number and
--       the browser would round it. If a new bigint column appears here it must
--       also be added to BIGINT_COLUMNS / BIGINT_ARRAY_COLUMNS in
--       supabase/functions/admin-api/index.ts, or the explorer will show ids
--       that are off by a few hundred.
-- select table_name, column_name, data_type
--   from information_schema.columns
--  where table_schema = 'public'
--    and data_type in ('bigint','bigint[]')
--  order by table_name, ordinal_position;

-- Reload PostgREST schema cache so new tables/columns are visible immediately
NOTIFY pgrst, 'reload schema';


-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
