-- ============================================================================
-- candy_v2 — migration 003: AI memory toggle
-- ============================================================================
-- Run AFTER schema.sql, seed_replies.sql, and migration_002_logging.sql.
-- Idempotent: safe to re-run.
--
-- Adds:
--   1. ai_memory_enabled on guild_settings   (per-guild memory on/off)
--
-- WHY THIS EXISTS
--   Memory was previously implicit: if ai_context_messages > 0 the bot read the
--   rolling per-channel summaries, and it always wrote them. There was no way to
--   say "chat with me, but do not remember this channel".
--
--   This flag is deliberately SEPARATE from ai_context_messages, because the two
--   answer different questions:
--     ai_context_messages -> how MANY past summaries to feed in
--     ai_memory_enabled   -> whether to keep and recall them AT ALL
--   Memory off means neither read nor write. Existing ai_interactions rows are
--   left untouched on purpose, so turning memory back on restores prior context
--   and past usage stats survive.
-- ============================================================================


alter table public.guild_settings
    add column if not exists ai_memory_enabled boolean not null default true;

comment on column public.guild_settings.ai_memory_enabled is
    'When false the AI neither reads nor writes per-channel summaries. Existing rows are left untouched, so re-enabling restores prior context.';


-- ============================================================================
-- VERIFY
-- ============================================================================
-- select guild_id, ai_enabled, ai_memory_enabled, ai_context_messages
--   from public.guild_settings;
--
-- Expect ai_memory_enabled = true for every existing row (the default), which
-- preserves today's behaviour until an admin turns it off.
