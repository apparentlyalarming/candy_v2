-- ============================================================================
-- candy_v2 — migration 002: logging, AI memory, and event feed
-- ============================================================================
-- Run AFTER schema.sql and seed_replies.sql.
-- Idempotent: safe to re-run.
--
-- Adds:
--   1. log_* columns on guild_settings        (server log channel config)
--   2. ai_interactions table                  (AI memory + analytics)
--   3. bot_events table                       (dashboard activity feed)
--   4. ai_stats_for_guild() RPC               (admins see stats, never text)
--   5. ai_interactions_trim() helper          (rolling per-channel window)
-- ============================================================================


-- ============================================================================
-- 1  GUILD SETTINGS — logging configuration
-- ============================================================================

alter table public.guild_settings
    add column if not exists log_enabled    boolean not null default true,
    add column if not exists log_categories text[]  not null default '{config,errors,qotd}';

comment on column public.guild_settings.log_channel_id is
    'Channel where the bot posts server event logs. NULL = logging off for this guild.';
comment on column public.guild_settings.log_categories is
    'Which event categories to log: config, commands, qotd, errors, members, ai.';

-- Constrain categories to the known set.
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


-- ============================================================================
-- 2  AI INTERACTIONS
-- ============================================================================
-- Stores a SHORT summary per exchange, not the full transcript.
--
-- Rationale (decided 2026-10-20): the Groq call returns structured output with
-- both the full user-facing reply and a one-line summary. Only the summary is
-- stored, because memory recall needs the gist, and summaries compress far
-- better than raw text. Fewer bytes on a 512 MB storage budget, and less user
-- text retained.
--
-- RLS (decided): a user can read their OWN rows across ALL servers. That is why
-- user_id references auth.users directly rather than being guild-scoped.

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
    'AI exchange summaries used for per-channel memory plus analytics. Full transcripts are never stored.';
comment on column public.ai_interactions.summary is
    'One-line gist of the exchange, produced by the model via JSON mode. NOT the full reply text.';

create index if not exists ai_interactions_channel_idx
    on public.ai_interactions (guild_id, channel_id, created_at desc);

create index if not exists ai_interactions_user_idx
    on public.ai_interactions (user_id, created_at desc);

create index if not exists ai_interactions_guild_idx
    on public.ai_interactions (guild_id, created_at desc);


-- ============================================================================
-- 3  BOT EVENTS (dashboard activity feed)
-- ============================================================================
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

create index if not exists bot_events_guild_idx
    on public.bot_events (guild_id, created_at desc);

create index if not exists bot_events_category_idx
    on public.bot_events (guild_id, category, created_at desc);


-- ============================================================================
-- 4  RLS
-- ============================================================================

alter table public.ai_interactions enable row level security;
alter table public.bot_events      enable row level security;
alter table public.ai_interactions force row level security;
alter table public.bot_events      force row level security;

-- ---------------------------------------------------------------------------
-- ai_interactions
--
-- User policy: read rows that belong to you, in ANY guild. This is the
-- "users see their own across all servers" rule.
-- ---------------------------------------------------------------------------
drop policy if exists "ai_interactions_select_own" on public.ai_interactions;
create policy "ai_interactions_select_own"
    on public.ai_interactions
    for select
    to authenticated
    using (user_id = auth.uid());

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


-- ============================================================================
-- 5  ADMIN STATS RPC — aggregates with NO message text
-- ============================================================================
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
-- Dev access to ALL AI summaries (across every guild).
-- An allowlist table, so granting is a data change rather than a deploy.
-- ---------------------------------------------------------------------------
create table if not exists public.dev_users (
    user_id    uuid primary key references auth.users(id) on delete cascade,
    note       text,
    created_at timestamptz not null default now()
);

comment on table public.dev_users is
    'Users allowed to read every ai_interactions row via is_dev(). Grant sparingly.';

alter table public.dev_users enable row level security;
alter table public.dev_users force row level security;

-- Users may check whether THEY are a dev (needed for UI gating). They may not
-- enumerate the list.
drop policy if exists "dev_users_select_self" on public.dev_users;
create policy "dev_users_select_self"
    on public.dev_users
    for select
    to authenticated
    using (user_id = auth.uid());

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
    'True when the current user is on the dev allowlist. Used for elevated dashboard views.';

revoke all on function public.is_dev() from public;
grant execute on function public.is_dev() to authenticated;

-- Dev read policy: sees every summary. Additive to the self-read policy.
drop policy if exists "ai_interactions_select_dev" on public.ai_interactions;
create policy "ai_interactions_select_dev"
    on public.ai_interactions
    for select
    to authenticated
    using (public.is_dev());


-- ============================================================================
-- 6  MEMORY WINDOW — rolling per-channel trim
-- ============================================================================
-- Decided 2026-10-20: instead of "delete everything after 1000 rows", keep the
-- last N summaries PER CHANNEL. Bounded by what the bot actually sends as
-- context, so storage stays predictable and recall stays recent.
--
-- The bot calls this ad hoc; no scheduled job is required.

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
    'Keeps only the newest p_keep summaries for a channel. Returns the number deleted.';

revoke all on function public.ai_trim_channel(bigint, bigint, integer) from public;
grant execute on function public.ai_trim_channel(bigint, bigint, integer) to authenticated, service_role;


-- ============================================================================
-- 7  REALTIME
-- ============================================================================
-- bot_events is intentionally NOT published: it is high-volume and the
-- dashboard can poll it. ai_interactions is NOT published either - it can
-- contain user text, and broadcasting it to subscribers is unnecessary.

do $$
begin
    -- nothing to add; documented so the omission is deliberate, not an oversight.
    raise notice 'ai_interactions and bot_events deliberately excluded from realtime publication.';
end $$;


-- ============================================================================
-- 8  VERIFY
-- ============================================================================
-- select column_name from information_schema.columns
--  where table_name = 'guild_settings' and column_name like 'log_%';

-- select tablename, rowsecurity from pg_tables
--  where schemaname='public' and tablename in ('ai_interactions','bot_events','dev_users');

-- select tablename, policyname from pg_policies
--  where tablename in ('ai_interactions','bot_events','dev_users');

-- To make yourself a dev (run in SQL editor, replacing the uuid):
-- insert into public.dev_users (user_id, note)
-- select id, 'owner' from auth.users where email = 'your@email.com'
-- on conflict (user_id) do nothing;

-- ============================================================================
-- END OF MIGRATION 002
-- ============================================================================
