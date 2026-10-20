-- ============================================================================
-- candy_v2 — migration 004: persistent AI memory (real turns)
-- ============================================================================
-- Run AFTER schema.sql, seed_replies.sql, migration_002_logging.sql and
-- migration_003_ai_memory.sql.
-- Idempotent: safe to re-run.
--
-- Adds:
--   1. guild_settings.ai_memory_days  (per-guild retention window, default 30)
--   2. guild_settings.ai_memory_ignored_channels (channels the AI never
--      records or recalls)
--   3. ai_memory table                (REAL prompt+reply turns, global per server)
--   4. ai_memory_prune() helper       (FIFO/turn-aware trim, called on write)
--
-- WHY THIS EXISTS
--   Until now the AI's "memory" was two things that did not survive a restart:
--     - an in-RAM deque per channel (raw turns, dies with the process), and
--     - ai_interactions, which stores only a one-line SUMMARY for analytics and
--       is never read back into a prompt.
--   The bot host restarts every 4 days for renewal, so the assistant began each
--   cycle amnesiac.
--
--   This migration adds the missing piece: a table of the ACTUAL user prompt and
--   assistant reply, so history can be reloaded into the deque on first use of a
--   guild after a restart.
--
-- DESIGN DECISIONS (settled with the operator)
--   * REAL TEXT, not summaries. A summary of an exchange is often LONGER than the
--     exchange and strictly vaguer, so summaries cost more tokens and deliver
--     less. Storing the real turn keeps token use flat versus today's deque.
--     NOTE: this deliberately REVERSES the original "no transcripts" stance for
--     this new table. ai_interactions still stores no message text and its RLS is
--     unchanged; the two tables answer different questions.
--   * GLOBAL PER SERVER. Retrieval filters on guild_id, not channel_id. The bot
--     remembers the server the way a person remembers a room. channel_id is still
--     stored on each row (for the ignore flag and for debugging) but is not the
--     lookup key.
--   * ONE ROW PER EXCHANGE, both halves. The prompt alone or the reply alone is
--     uninterpretable; the pair is the unit.
--   * RETENTION IS A TIME WINDOW, per guild, default 30 days, and it is
--     PER-ROW: each row expires on its own Nth birthday. There is deliberately NO
--     table wipe. 0 means "keep forever".
-- ============================================================================


-- ============================================================================
-- 1  GUILD SETTINGS — retention window + per-channel ignore list
-- ============================================================================

alter table public.guild_settings
    add column if not exists ai_memory_days integer not null default 30;

alter table public.guild_settings
    add column if not exists ai_memory_ignored_channels bigint[] not null default '{}';

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

comment on column public.guild_settings.ai_memory_days is
    'How many days of AI memory to keep, per row (each row expires on its own Nth birthday). 0 = keep forever. Default 30.';
comment on column public.guild_settings.ai_memory_ignored_channels is
    'Channels the AI neither records nor recalls. Per-channel escape hatch for channels where memory must stay off.';


-- ============================================================================
-- 2  ai_memory — the real conversation turns
-- ============================================================================
-- One row per exchange. Both halves are NOT NULL: a reply with no prompt cannot
-- be fed back into a conversation, so a half-row is a bug rather than something
-- to tolerate.
--
-- user_id references auth.users and is left NULL by the bot (it knows the
-- Discord id, not the linked Supabase account). discord_user_id is the practical
-- key and is what the personalized (!candyp) lookup uses.

create table if not exists public.ai_memory (
    id                  bigserial primary key,
    guild_id            bigint not null references public.guilds(id) on delete cascade,
    channel_id          bigint,                     -- where it happened; NOT the lookup key
    user_id             uuid references auth.users(id) on delete set null,
    discord_user_id     bigint,                     -- the prompt author
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

-- Guild-scoped reads, newest first (the common query).
create index if not exists ai_memory_guild_created_idx
    on public.ai_memory (guild_id, created_at desc);

-- Per-person reads for !candyp.
create index if not exists ai_memory_guild_user_created_idx
    on public.ai_memory (guild_id, discord_user_id, created_at desc);

-- Retention sweep by age.
create index if not exists ai_memory_created_idx
    on public.ai_memory (created_at);


-- ============================================================================
-- 3  ai_memory_prune() — retention + FIFO trim, one pass
-- ============================================================================
-- Called by the bot after each successful write, exactly like ai_trim_channel().
-- Two jobs in one call so storage cannot drift:
--   1. AGE: delete rows older than p_keep_days (skipped when p_keep_days = 0,
--      which means "keep forever").
--   2. (No count cap: the operator opted for a time window only.)
--
-- SECURITY DEFINER + an explicit service_role/dev check, mirroring
-- ai_trim_channel(). Regular authenticated users have no business pruning.

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
    -- is_dev() branch is what lets an operator run this by hand in the SQL editor.
    if auth.uid() is not null and not public.is_dev() then
        raise exception 'not authorized' using errcode = '42501';
    end if;

    if p_keep_days is null or p_keep_days <= 0 then
        -- 0 (or negative) = keep forever. Nothing to do.
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
-- 4  ROW LEVEL SECURITY
-- ============================================================================
-- Same rule as the rest of the schema: RLS on, and a table with no policy is
-- deny-all, which is the safe default. The bot writes with service_role and
-- bypasses RLS by design.
--
-- Users get SELECT only for guilds they manage, so the dashboard can show that
-- memory exists. There is deliberately NO insert/update/delete policy: memory is
-- written and pruned by the bot alone, and a dashboard never edits a transcript.

alter table public.ai_memory enable row level security;
alter table public.ai_memory force row level security;

drop policy if exists "ai_memory_select_managed" on public.ai_memory;
create policy "ai_memory_select_managed"
    on public.ai_memory
    for select
    to authenticated
    using (public.can_manage_guild(guild_id));


-- ============================================================================
-- VERIFY
-- ============================================================================
-- select guild_id, ai_memory_enabled, ai_memory_days, ai_memory_ignored_channels
--   from public.guild_settings;
--
-- Expect ai_memory_days = 30 and an empty ignore array on every existing row,
-- which preserves today's behaviour (memory on, 30-day window) until an admin
-- changes it.
--
--   select count(*) from public.ai_memory;   -- 0 right after the migration
