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
        check (qotd_days <@ array[1,2,3,4,5,6,7]::integer[])
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
    'Written ONLY by the sync-guilds Edge Function / service_role. Users must never insert here - RLS trusts this table completely.';


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

-- Fast "next unasked question" lookup + ordering
create index if not exists qotd_questions_queue_idx
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
          and (
                ug.is_admin
                or (ug.permissions & 32) = 32      -- 32 = MANAGE_GUILD
                or (ug.permissions & 8)  = 8       -- 8  = ADMINISTRATOR
              )
    );
$$;

comment on function public.can_manage_guild(bigint) is
    'True when auth.uid() has MANAGE_GUILD (32) or ADMINISTRATOR (8) in the given guild. SECURITY DEFINER breaks RLS recursion.';

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

drop policy if exists "guild_settings_insert_managed" on public.guild_settings;
create policy "guild_settings_insert_managed"
    on public.guild_settings
    for insert
    to authenticated
    with check (public.can_manage_guild(guild_id));

drop policy if exists "guild_settings_update_managed" on public.guild_settings;
create policy "guild_settings_update_managed"
    on public.guild_settings
    for update
    to authenticated
    using (public.can_manage_guild(guild_id))
    with check (public.can_manage_guild(guild_id));

-- No delete policy: settings rows are never deleted by users (cascade handles guild removal).

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
begin
    if not public.can_manage_guild(p_guild_id) then
        raise exception 'not authorized for guild %', p_guild_id
            using errcode = '42501';
    end if;

    if p_question is null or length(btrim(p_question)) = 0 then
        raise exception 'question must not be blank' using errcode = '22023';
    end if;

    select coalesce(max(position), 0) + 1
      into v_next_position
      from public.qotd_questions
     where guild_id = p_guild_id;

    insert into public.qotd_questions (guild_id, question, position, created_by)
    values (p_guild_id, btrim(p_question), v_next_position, auth.uid())
    returning * into v_row;

    return v_row;
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
-- 08  VERIFICATION QUERIES
-- ============================================================================
-- Run these after the script to confirm everything landed.

-- 08.1 Tables exist (expect 10 rows)
-- select tablename
--   from pg_tables
--  where schemaname = 'public'
--    and tablename in ('guilds','guild_settings','user_guilds',
--                      'reply_actions','guild_replies','qotd_questions',
--                      'ai_memory','ai_interactions','bot_events','dev_users')
--  order by tablename;

-- 08.2 RLS enabled on all 10 (expect rls_enabled = true everywhere)
-- select relname as table_name, relrowsecurity as rls_enabled, relforcerowsecurity as rls_forced
--   from pg_class
--  where relname in ('guilds','guild_settings','user_guilds',
--                    'reply_actions','guild_replies','qotd_questions',
--                    'ai_memory','ai_interactions','bot_events','dev_users');

-- 08.3 Policy count per table (expect a sensible number, not 0)
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
--           ('qotd_days'),('qotd_empty_queue_notice'),('qotd_last_fired_at')
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

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
