// ============================================================================
// Edge Function: sync-guilds
// ============================================================================
// Returns the Discord servers the caller may manage, and refreshes the
// `user_guilds` table so Row Level Security has current permission rows.
//
// WHAT CHANGED AND WHY
//   This used to build the list from the caller's Discord OAuth token
//   (`provider_token`), which the browser supplies. That made permission
//   revocation the client's job: a demoted admin simply never called this
//   function and kept their `user_guilds` row — and therefore database write
//   access — forever.
//
//   It now asks Discord with the BOT token instead. The bot is in exactly the
//   guilds the dashboard can serve, so "the bot's guild list, filtered by
//   whether this Discord user is an admin in each" is the same set, derived
//   from an authority the caller cannot avoid.
//
//   Combined with the 10-minute TTL in `can_manage_guild()` and the
//   apply-guild-settings write gate, a stale row is no longer a standing grant.
//
// AUTH
//   Supabase user JWT only. No provider_token is needed or accepted.
//
// DEPLOY
//   supabase functions deploy sync-guilds --use-api
//   Requires the DISCORD_BOT_TOKEN secret.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  DISCORD_API,
  DISCORD_TIMEOUT_MS,
  fetchWithTimeout,
  getDiscordUserId,
  listBotGuilds,
  verifyGuildAdmin,
} from "../_shared/discord_verify.ts";

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

/** Only resolve channels/roles for this many guilds; Discord rate-limits per guild. */
const DETAIL_LIMIT = 10;

/** Verify at most this many guilds at once. */
const VERIFY_CONCURRENCY = 4;

interface DiscordChannel {
  id: string;
  name: string;
  type: number;
  position?: number;
}

interface DiscordRole {
  id: string;
  name: string;
  color: number;
  position: number;
}

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

/** Run tasks with bounded concurrency so we do not trip Discord's rate limits. */
async function mapLimit<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor++;
      out[index] = await fn(items[index]);
    }
  });
  await Promise.all(workers);
  return out;
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS_HEADERS });
  if (req.method !== "POST") return errorResponse(405, "method_not_allowed", "Use POST.");

  // --------------------------------------------------------------------
  // 1. Authenticate the Supabase user
  // --------------------------------------------------------------------
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
    return errorResponse(
      500,
      "server_misconfigured",
      "DISCORD_BOT_TOKEN is not set. Run: supabase secrets set DISCORD_BOT_TOKEN=...",
    );
  }

  const admin = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  // The auth server validates the signature; we never decode the JWT ourselves.
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
  // 2. Which Discord account is this?
  // --------------------------------------------------------------------
  const discordUserId = await getDiscordUserId(jwt, supabaseUrl, anonKey);
  if (!discordUserId) {
    return errorResponse(
      409,
      "no_discord_identity",
      "This account has no linked Discord identity. Log out and sign in with Discord.",
    );
  }

  // Remember the mapping: the bot needs it to revoke access from gateway events
  // (auth.users is not reachable from PostgREST).
  const { error: profileErr } = await admin
    .from("profiles")
    .upsert(
      { user_id: userId, discord_user_id: discordUserId, updated_at: new Date().toISOString() },
      { onConflict: "user_id" },
    );
  if (profileErr) {
    // Non-fatal: everything below still works, only bot revocation is delayed.
    console.error("sync-guilds profile upsert failed:", profileErr.message);
  }

  // --------------------------------------------------------------------
  // 3. Enumerate the bot's guilds and check membership/permissions in each
  // --------------------------------------------------------------------
  let botGuilds: Awaited<ReturnType<typeof listBotGuilds>>;
  try {
    botGuilds = await listBotGuilds(botToken);
  } catch (err) {
    return errorResponse(502, "discord_error", `Could not list bot guilds: ${String(err)}`);
  }

  const checks = await mapLimit(botGuilds, VERIFY_CONCURRENCY, async (guild) => {
    const result = await verifyGuildAdmin(botToken, guild.id, discordUserId);
    return { guild, result };
  });

  const manageable = checks.filter((c) => c.result.ok);

  // --------------------------------------------------------------------
  // 4. Write guilds + user_guilds with the service role
  // --------------------------------------------------------------------
  const nowIso = new Date().toISOString();

  if (manageable.length > 0) {
    const guildRows = manageable.map(({ guild, result }) => ({
      id: guild.id,
      name: result.guild?.name ?? guild.name,
      icon_url: guild.icon
        ? `https://cdn.discordapp.com/icons/${guild.id}/${guild.icon}.png?size=128`
        : null,
      updated_at: nowIso,
    }));
    // bot_present is owned by the bot, so it is deliberately not written here.
    const { error: guildErr } = await admin
      .from("guilds")
      .upsert(guildRows, { onConflict: "id", ignoreDuplicates: false });
    if (guildErr) return errorResponse(500, "db_guilds_failed", guildErr.message);

    const membershipRows = manageable.map(({ guild, result }) => ({
      user_id: userId,
      guild_id: guild.id,
      is_admin: result.isAdmin,
      // Decimal string: the bitmask exceeds 2^53, and a JS number would lose
      // precision. PostgREST coerces the string into bigint exactly.
      permissions: result.permissions,
      synced_at: nowIso,
    }));
    const { error: memberErr } = await admin
      .from("user_guilds")
      .upsert(membershipRows, { onConflict: "user_id,guild_id" });
    if (memberErr) return errorResponse(500, "db_members_failed", memberErr.message);
  }

  // Prune this user's rows for guilds where they are no longer a member/admin.
  // Scoped to user_id, so one admin's sync can never touch another's rows.
  const keepIds = manageable.map((m) => m.guild.id);
  let deleteQuery = admin.from("user_guilds").delete().eq("user_id", userId);
  if (keepIds.length > 0) {
    deleteQuery = deleteQuery.not("guild_id", "in", `(${keepIds.join(",")})`);
  }
  const { error: pruneErr } = await deleteQuery;
  if (pruneErr) console.error("sync-guilds prune failed:", pruneErr.message);

  // --------------------------------------------------------------------
  // 5. Fetch channels + roles (bot token) for the first few guilds
  // --------------------------------------------------------------------
  const withDetail = await mapLimit(
    manageable.slice(0, DETAIL_LIMIT),
    VERIFY_CONCURRENCY,
    async ({ guild, result }) => {
      let channels: Array<{ id: string; name: string; type: number }> = [];
      let roles: Array<{ id: string; name: string; color: number }> = [];
      const headers = {
        Authorization: `Bot ${botToken}`,
        "User-Agent": "DiscordBot (https://candy-web-beryl.vercel.app, 1.0)",
      };
      try {
        const [chRes, roleRes] = await Promise.all([
          fetchWithTimeout(`${DISCORD_API}/guilds/${guild.id}/channels`, { headers }, DISCORD_TIMEOUT_MS),
          fetchWithTimeout(`${DISCORD_API}/guilds/${guild.id}/roles`, { headers }, DISCORD_TIMEOUT_MS),
        ]);
        if (chRes.ok) {
          const raw: DiscordChannel[] = await chRes.json();
          channels = raw
            .filter((c) => c.type === 0 || c.type === 2 || c.type === 5)
            .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
            .map((c) => ({ id: c.id, name: c.name, type: c.type }));
        }
        if (roleRes.ok) {
          const raw: DiscordRole[] = await roleRes.json();
          roles = raw
            .filter((r) => r.name !== "@everyone")
            .sort((a, b) => b.position - a.position)
            .map((r) => ({ id: r.id, name: r.name, color: r.color }));
        }
      } catch {
        // Degrade to empty lists rather than inventing data.
      }
      return {
        id: guild.id,
        name: result.guild?.name ?? guild.name,
        icon_url: guild.icon
          ? `https://cdn.discordapp.com/icons/${guild.id}/${guild.icon}.png?size=128`
          : null,
        is_admin: result.isAdmin,
        permissions: result.permissions,
        channels,
        roles,
      };
    },
  );

  // --------------------------------------------------------------------
  // 6. Merge bot_present from the database and return
  // --------------------------------------------------------------------
  // `id::text` is not cosmetic. PostgREST serialises bigint as a JSON number,
  // and Discord snowflakes exceed 2^53, so 1482658029542576210 comes back as
  // 1482658029542576000. The lookup then misses and every guild reports
  // bot_present:false, which the dashboard reads as "no servers". Casting in
  // SQL keeps the id exact.
  const { data: botRows, error: botErr } = await admin
    .from("guilds")
    .select("id::text,bot_present")
    .in("id", manageable.map((m) => m.guild.id));
  if (botErr) console.error("sync-guilds bot_present lookup failed:", botErr.message);
  const botPresent = new Map<string, boolean>(
    (botRows ?? []).map((r: { id: unknown; bot_present: unknown }) => [
      String(r.id),
      Boolean(r.bot_present),
    ]),
  );

  return json({
    guilds: withDetail.map((g) => ({ ...g, bot_present: botPresent.get(g.id) ?? false })),
    synced_at: nowIso,
  });
});
