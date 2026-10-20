-- ============================================================================
-- candy_v2 — Supabase schema + RLS
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
-- SECTIONS:
--   01  Extensions
--   02  Tables
--   03  Indexes
--   04  RLS helper functions
--   05  Row Level Security policies
--   06  Realtime / replication
--   07  Seed data (reply action catalog)
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
    log_channel_id          bigint,

    -- chat AI ---------------------------------------------------------------
    ai_enabled              boolean not null default false,
    ai_model                text    not null default 'llama-3.3-70b-versatile',
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
comment on column public.guild_settings.ai_channel_ids is
    'Channels where AI responds. Empty array = disabled (see ai_all_channels for everywhere).';
comment on column public.guild_settings.ai_memory_enabled is
    'When false the AI neither reads nor writes memory. Existing rows are left untouched, so re-enabling restores prior context.';
comment on column public.guild_settings.ai_memory_days is
    'How many days of AI memory to keep, per row (each row expires on its own Nth birthday). 0 = keep forever. Default 30.';
comment on column public.guild_settings.ai_memory_ignored_channels is
    'Channels the AI neither records nor recalls. Per-channel escape hatch for channels where memory must stay off.';
comment on column public.guild_settings.qotd_days is
    'Days of week QOTD fires: 1=Mon..7=Sun. Empty array = every day.';


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
    'Global shipped catalog of fun actions with default reply templates. Public read, no user writes.';
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
-- This deliberately REVERSES the original no-transcripts stance (see
-- migration_004_ai_memory.sql). ai_interactions still stores no message text;
-- that table is analytics, this one is memory.

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
comment on column public.ai_memory.user_content is
    'The user prompt. Stored deliberately: a reply is uninterpretable without the prompt it answers.';


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

-- Belt and braces: also force RLS for table owners where applicable.
alter table public.guilds          force row level security;
alter table public.guild_settings  force row level security;
alter table public.user_guilds     force row level security;
alter table public.reply_actions   force row level security;
alter table public.guild_replies   force row level security;
alter table public.qotd_questions  force row level security;
alter table public.ai_memory       force row level security;

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
-- birthday. There is deliberately NO table wipe.
--
-- p_keep_days <= 0 means "keep forever" and is a no-op.

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
    'Deletes ai_memory rows older than p_keep_days for one guild. Per-row expiry, never a table wipe. p_keep_days <= 0 keeps forever.';

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
exception
    when undefined_object then
        raise notice 'Publication supabase_realtime not found - enable Realtime in the dashboard instead.';
end $$;


-- ============================================================================
-- 07  SEED DATA — fun action reply catalog
-- ============================================================================
-- Templates use {user} and {target}. NEVER raw <@id> mentions.
-- Add more actions here and re-run; keys are upserted.

insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values
    ('hug', 'Hug', '🤗', 10, array[
        '{user} hugs {target} tightly',
        '{user} wraps {target} in a warm hug',
        '{user} gives {target} the softest hug ever',
        '{user} pulls {target} into a big bear hug',
        '{user} hugs {target} and refuses to let go'
    ]),
    ('pat', 'Pat', '🫳', 20, array[
        '{user} pats {target} on the head',
        '{user} gives {target} gentle headpats',
        '{user} pats {target} softly, twice',
        '{user} showers {target} with headpats'
    ]),
    ('poke', 'Poke', '👉', 30, array[
        '{user} pokes {target}',
        '{user} pokes {target} right in the cheek',
        '{user} pokes {target} repeatedly until they respond',
        '{user} pokes {target} with zero remorse'
    ]),
    ('cuddle', 'Cuddle', '🫂', 40, array[
        '{user} cuddles up to {target}',
        '{user} and {target} are now a cuddle puddle',
        '{user} pulls {target} into a cozy cuddle',
        '{user} cuddles {target} until they fall asleep'
    ]),
    ('boop', 'Boop', '👆', 50, array[
        '{user} boops {target} on the nose',
        '{user} gives {target} a tiny boop',
        '{user} boops {target}. boop!',
        '{user} sneaks up and boops {target}'
    ]),
    ('highfive', 'High Five', '🖐️', 60, array[
        '{user} high-fives {target}!',
        '{user} and {target} slap hands in perfect sync',
        '{user} goes for the high five and {target} actually catches it',
        '{user} high-fives {target} so hard it echoes'
    ]),
    ('wave', 'Wave', '👋', 70, array[
        '{user} waves at {target}',
        '{user} waves enthusiastically at {target}',
        '{user} gives {target} a little wave',
        '{user} waves at {target} from across the room'
    ]),
    ('dance', 'Dance', '💃', 80, array[
        '{user} drags {target} onto the dance floor',
        '{user} and {target} are dancing like nobody is watching',
        '{user} shows {target} their best moves',
        '{user} spins {target} around and dips them'
    ]),
    ('slap', 'Slap', '✋', 90, array[
        '{user} slaps {target} with a trout',
        '{user} slaps {target}. ouch!',
        '{user} slaps {target} into next week',
        '{user} delivers a dramatic slap to {target}'
    ]),
    ('kick', 'Kick', '🦵', 100, array[
        '{user} kicks {target} into orbit',
        '{user} kicks {target} gently (affectionate)',
        '{user} winds up and kicks {target}',
        '{user} kicks {target} straight out the door'
    ]),
    ('bite', 'Bite', '😬', 110, array[
        '{user} bites {target}',
        '{user} takes a small nibble out of {target}',
        '{user} chomps {target} harmlessly',
        '{user} bites {target} - purely a crime of passion'
    ]),
    ('bonk', 'Bonk', '🔨', 120, array[
        '{user} bonks {target} on the head',
        '{user} sends {target} to horny jail with a bonk',
        '{user} bonks {target}. go to jail.',
        '{user} applies a firm bonk to {target}'
    ])
on conflict (key) do update
    set label             = excluded.label,
        emoji             = excluded.emoji,
        sort_order        = excluded.sort_order,
        default_templates = excluded.default_templates;


-- ============================================================================
-- 08  VERIFICATION QUERIES
-- ============================================================================
-- Run these after the script to confirm everything landed.

-- 08.1 Tables exist (expect 7 rows)
-- select tablename
--   from pg_tables
--  where schemaname = 'public'
--    and tablename in ('guilds','guild_settings','user_guilds',
--                      'reply_actions','guild_replies','qotd_questions',
--                      'ai_memory')
--  order by tablename;

-- 08.2 RLS enabled on all 7 (expect rls_enabled = true everywhere)
-- select relname as table_name, relrowsecurity as rls_enabled, relforcerowsecurity as rls_forced
--   from pg_class
--  where relname in ('guilds','guild_settings','user_guilds',
--                    'reply_actions','guild_replies','qotd_questions',
--                    'ai_memory');

-- 08.3 Policy count per table (expect a sensible number, not 0)
-- select tablename, count(*) as policy_count
--   from pg_policies
--  where schemaname = 'public'
--  group by tablename
--  order by tablename;

-- 08.4 Seed catalog loaded (expect 12 rows)
-- select key, label, array_length(default_templates, 1) as template_count
--   from public.reply_actions
--  order by sort_order;

-- 08.5 Helper function exists
-- select proname, prosecdef as is_security_definer
--   from pg_proc
--  where proname in ('can_manage_guild','qotd_add_question',
--                    'qotd_move_question','qotd_renumber','ai_memory_prune');

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
