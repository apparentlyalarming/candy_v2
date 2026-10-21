// ============================================================================
// Edge Function: apply-guild-settings
// ============================================================================
// The ONLY path that can change `guild_settings`. Direct UPDATE is revoked from
// `anon` and `authenticated` in schema.sql, so this cannot be bypassed with a
// stolen JWT and PostgREST: every write re-verifies with Discord, using the bot
// token, at the moment of the request.
//
// WHY A GATE AND NOT A PRE-CHECK
//   A client that "refreshes permissions then writes" is two calls; an attacker
//   skips the first. Here the verification and the write are the same request,
//   so the only thing that can produce a write is a fresh Discord answer.
//
// CONFLICT DETECTION
//   `expected_updated_at` is the optimistic-lock token the dashboard loaded.
//   The UPDATE only matches while the row still carries that stamp, so a save
//   that would clobber another admin's edit returns 409 instead of winning.
//
// DEPLOY
//   supabase functions deploy apply-guild-settings --use-api
//   Requires the DISCORD_BOT_TOKEN secret.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { fetchWithTimeout, getDiscordUserId, verifyGuildAdmin } from "../_shared/discord_verify.ts";

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

/**
 * Columns the dashboard is allowed to write.
 *
 * An allowlist, not a denylist: without it a caller could try to rewrite
 * `guild_id`, `created_at`, or `updated_at` (which would defeat the optimistic
 * lock). Anything not listed is silently dropped.
 */
const EDITABLE_COLUMNS = new Set<string>([
  "prefix",
  "prefix_enabled",
  "log_channel_id",
  "log_enabled",
  "log_categories",
  "ai_enabled",
  "ai_model",
  "ai_system_prompt",
  "ai_max_tokens",
  "ai_temperature",
  "ai_context_messages",
  "ai_memory_enabled",
  "ai_memory_days",
  "ai_memory_ignored_channels",
  "ai_user_cooldown_seconds",
  "ai_all_channels",
  "ai_channel_ids",
  "qotd_enabled",
  "qotd_channel_id",
  "qotd_role_id",
  "qotd_time",
  "qotd_timezone",
  "qotd_days",
  "qotd_empty_queue_notice",
]);

function errorResponse(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ error: { code, message } }), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS_HEADERS });
  if (req.method !== "POST") return errorResponse(405, "method_not_allowed", "Use POST.");

  const jwt = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
  if (!jwt) return errorResponse(401, "missing_auth", "Missing Authorization header.");

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const botToken = Deno.env.get("DISCORD_BOT_TOKEN");
  if (!supabaseUrl || !anonKey || !serviceKey) {
    return errorResponse(500, "server_misconfigured", "Supabase env missing.");
  }
  if (!botToken) {
    return errorResponse(500, "server_misconfigured", "DISCORD_BOT_TOKEN is not set.");
  }

  // --------------------------------------------------------------------
  // 1. Authenticate
  // --------------------------------------------------------------------
  let userId: string | null = null;
  try {
    const res = await fetchWithTimeout(
      `${supabaseUrl.replace(/\/+$/, "")}/auth/v1/user`,
      { method: "GET", headers: { Authorization: `Bearer ${jwt}`, apikey: anonKey } },
      8_000,
    );
    if (res.ok) {
      const user = await res.json();
      if (typeof user?.id === "string" && user.id) userId = user.id;
    }
  } catch {
    // handled below
  }
  if (!userId) return errorResponse(401, "invalid_auth", "Invalid or expired token.");

  // --------------------------------------------------------------------
  // 2. Parse + validate the request
  // --------------------------------------------------------------------
  let guildId = "";
  let changes: Record<string, unknown> = {};
  let expectedUpdatedAt: string | null = null;
  try {
    const body = await req.json();
    if (typeof body?.guild_id === "string" || typeof body?.guild_id === "number") {
      guildId = String(body.guild_id);
    }
    if (body?.changes && typeof body.changes === "object") {
      changes = body.changes as Record<string, unknown>;
    }
    if (typeof body?.expected_updated_at === "string") {
      expectedUpdatedAt = body.expected_updated_at;
    }
  } catch {
    return errorResponse(400, "bad_json", "Request body must be valid JSON.");
  }

  if (!/^\d{5,25}$/.test(guildId)) {
    return errorResponse(400, "bad_guild_id", "guild_id must be a Discord snowflake.");
  }

  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(changes)) {
    if (EDITABLE_COLUMNS.has(key)) payload[key] = value;
  }
  if (Object.keys(payload).length === 0) {
    return errorResponse(400, "no_changes", "No editable columns were supplied.");
  }

  // --------------------------------------------------------------------
  // 3. Verify with Discord, right now
  // --------------------------------------------------------------------
  const discordUserId = await getDiscordUserId(jwt, supabaseUrl, anonKey);
  if (!discordUserId) {
    return errorResponse(
      409,
      "no_discord_identity",
      "This account has no linked Discord identity. Log out and sign in with Discord.",
    );
  }

  const admin = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const verdict = await verifyGuildAdmin(botToken, guildId, discordUserId);

  if (!verdict.ok) {
    // Revoke the stale cache immediately, so the next read is refused too
    // rather than merely this write.
    if (verdict.reason === "not_member" || verdict.reason === "no_permission") {
      await admin
        .from("user_guilds")
        .delete()
        .eq("user_id", userId)
        .eq("guild_id", guildId);
    }
    const message = verdict.reason === "not_member"
      ? "You are no longer a member of that server."
      : verdict.reason === "guild_unknown"
      ? "The bot is not in that server, so its settings cannot be changed."
      : verdict.reason === "no_permission"
      ? "You no longer have Manage Server on that server."
      : "Discord could not confirm your permissions right now. Try again.";
    return errorResponse(403, verdict.reason ?? "not_allowed", message);
  }

  // Verification just succeeded, so refresh the cache row's TTL. This is what
  // keeps an active admin from being locked out by can_manage_guild()'s window.
  const nowIso = new Date().toISOString();
  const { error: touchErr } = await admin
    .from("user_guilds")
    .upsert(
      {
        user_id: userId,
        guild_id: guildId,
        is_admin: verdict.isAdmin,
        permissions: verdict.permissions,
        synced_at: nowIso,
      },
      { onConflict: "user_id,guild_id" },
    );
  if (touchErr) console.error("apply-guild-settings touch failed:", touchErr.message);

  // Keep the guild's display row honest too (name/icon may have changed).
  if (verdict.guild) {
    await admin.from("guilds").upsert(
      {
        id: guildId,
        name: verdict.guild.name,
        icon_url: verdict.guild.icon
          ? `https://cdn.discordapp.com/icons/${guildId}/${verdict.guild.icon}.png?size=128`
          : null,
        updated_at: nowIso,
      },
      { onConflict: "id", ignoreDuplicates: false },
    );
  }

  // --------------------------------------------------------------------
  // 4. Write, guarded by the optimistic-lock stamp
  // --------------------------------------------------------------------
  let update = admin.from("guild_settings").update(payload).eq("guild_id", guildId);
  if (expectedUpdatedAt) update = update.eq("updated_at", expectedUpdatedAt);

  const { data, error } = await update.select("updated_at");
  if (error) {
    // Constraint violations are the caller's fault, not a server fault.
    const status = error.code === "23514" || error.code === "22001" || error.code === "22003" ? 400 : 500;
    return errorResponse(status, error.code ?? "db_error", error.message);
  }

  if ((data?.length ?? 0) === 0) {
    if (expectedUpdatedAt) {
      const { data: current } = await admin
        .from("guild_settings")
        .select("updated_at")
        .eq("guild_id", guildId)
        .maybeSingle();
      return errorResponse(
        409,
        "concurrent_edit",
        current?.updated_at
          ? `Another admin saved this server's settings first (at ${current.updated_at}). Reload to see their changes, then reapply yours.`
          : "Another admin changed this server's settings while you had the page open.",
      );
    }
    return errorResponse(404, "settings_missing", "No settings row exists for that server yet.");
  }

  return json({ updated_at: data[0].updated_at });
});
