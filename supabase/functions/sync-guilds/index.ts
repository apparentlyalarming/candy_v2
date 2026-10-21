// ============================================================================
// Edge Function: sync-guilds
// ============================================================================
// Returns the Discord servers the caller can manage, and refreshes the
// `user_guilds` table so Row Level Security has accurate permission rows.
//
// WHY THIS EXISTS
//   The dashboard is a browser and holds only a user token. Discord's guild
//   list (and the MANAGE_GUILD bit) cannot be read from the database, and the
//   `user_guilds` table must be written with elevated rights. This function is
//   the one place that bridges the two.
//
// AUTH
//   The caller must present a valid Supabase user JWT (the logged-in user's
//   access token). The Discord access token is supplied in the body as
//   `provider_token` — it is the token Discord issued during OAuth, which the
//   browser received from Supabase and never stores server-side. We use it once,
//   for this request, and never persist it.
//
// SECURITY
//   - The Discord token is used to call Discord, then discarded. It is never
//     written to the database, never logged, and never echoed back.
//   - guild_id values come from Discord, not from the caller, so a user cannot
//     claim a guild they are not in.
//   - The service-role client bypasses RLS deliberately: this function is the
//     trusted writer of `user_guilds`. Every row it writes is derived from
//     Discord's own answer for the authenticated user.
//
// DEPLOY
//   supabase functions deploy sync-guilds
//   (SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY are injected
//    automatically; no extra secrets are required.)
//
// The dashboard calls: {SUPABASE_URL}/functions/v1/sync-guilds
// with { provider_token } and the user's access token in Authorization.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const DISCORD_API = "https://discord.com/api/v10";

/** Discord permission bits we care about. */
const ADMINISTRATOR = 0x8n; // 8
const MANAGE_GUILD = 0x20n; // 32

const DISCORD_TIMEOUT_MS = 15_000;

interface DiscordGuild {
  id: string;
  name: string;
  icon: string | null;
  owner?: boolean;
  permissions?: string;
}

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

function errorResponse(
  status: number,
  code: string,
  message: string,
): Response {
  return new Response(
    JSON.stringify({ error: { code, message } }),
    { status, headers: { ...CORS_HEADERS, "Content-Type": "application/json" } },
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

/** fetch with a hard timeout so a hung Discord call cannot pin the worker. */
async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return errorResponse(405, "method_not_allowed", "Use POST.");
  }

  // --------------------------------------------------------------------
  // 1. Authenticate the Supabase user
  // --------------------------------------------------------------------
  const authHeader = req.headers.get("Authorization") ?? "";
  const jwt = authHeader.replace(/^Bearer\s+/i, "").trim();
  if (!jwt) {
    return errorResponse(401, "missing_auth", "Missing Authorization header.");
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !anonKey || !serviceKey) {
    return errorResponse(500, "server_misconfigured", "Supabase env missing.");
  }

  // Verify the token with Supabase's auth server; never trust a decoded JWT.
  let userId: string | null = null;
  try {
    const res = await fetchWithTimeout(
      `${supabaseUrl.replace(/\/+$/, "")}/auth/v1/user`,
      { method: "GET", headers: { Authorization: `Bearer ${jwt}`, apikey: anonKey } },
      10_000,
    );
    if (res.ok) {
      const user = await res.json();
      if (typeof user?.id === "string" && user.id) userId = user.id;
    }
  } catch {
    // fall through to the null check below
  }
  if (!userId) {
    return errorResponse(401, "invalid_auth", "Invalid or expired token.");
  }

  // --------------------------------------------------------------------
  // 2. Read the Discord provider token from the body
  // --------------------------------------------------------------------
  let providerToken = "";
  try {
    const body = await req.json();
    if (typeof body?.provider_token === "string") providerToken = body.provider_token;
  } catch {
    return errorResponse(400, "bad_json", "Request body must be valid JSON.");
  }
  if (!providerToken) {
    return errorResponse(
      400,
      "missing_provider_token",
      "provider_token is required. Log out and back in to refresh your Discord token.",
    );
  }

  const discordHeaders = { Authorization: `Bearer ${providerToken}` };

  // --------------------------------------------------------------------
  // 3. Ask Discord which guilds this user is in
  // --------------------------------------------------------------------
  let guilds: DiscordGuild[];
  try {
    const res = await fetchWithTimeout(
      `${DISCORD_API}/users/@me/guilds`,
      { headers: discordHeaders },
      DISCORD_TIMEOUT_MS,
    );
    if (res.status === 401) {
      return errorResponse(
        401,
        "discord_token_invalid",
        "Your Discord token expired. Log out and back in to refresh it.",
      );
    }
    if (!res.ok) {
      return errorResponse(502, "discord_error", `Discord returned ${res.status}.`);
    }
    guilds = await res.json();
    if (!Array.isArray(guilds)) guilds = [];
  } catch {
    return errorResponse(504, "discord_timeout", "Discord did not respond in time.");
  }

  // --------------------------------------------------------------------
  // 4. Keep only guilds where the user can actually manage settings
  // --------------------------------------------------------------------
  const manageable = guilds.filter((g) => {
    if (g.owner) return true;
    let perms = 0n;
    try {
      perms = BigInt(g.permissions ?? "0");
    } catch {
      perms = 0n;
    }
    return (perms & ADMINISTRATOR) === ADMINISTRATOR || (perms & MANAGE_GUILD) === MANAGE_GUILD;
  });

  // --------------------------------------------------------------------
  // 5. Write guilds + user_guilds with the service role
  // --------------------------------------------------------------------
  const admin = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const nowIso = new Date().toISOString();

  // guilds: identity/name/icon. bot_present is owned by the bot, so we do NOT
  // overwrite it here — a dashboard sync must not claim the bot left or joined.
  if (manageable.length > 0) {
    const guildRows = manageable.map((g) => ({
      id: g.id,
      name: g.name,
      icon_url: g.icon
        ? `https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png?size=128`
        : null,
      updated_at: nowIso,
    }));
    const { error: guildErr } = await admin
      .from("guilds")
      .upsert(guildRows, { onConflict: "id", ignoreDuplicates: false });
    if (guildErr) {
      return errorResponse(500, "db_guilds_failed", guildErr.message);
    }

    const membershipRows = manageable.map((g) => ({
      user_id: userId,
      guild_id: g.id,
      is_admin: Boolean(g.owner) ||
        ((() => {
          try {
            return (BigInt(g.permissions ?? "0") & ADMINISTRATOR) === ADMINISTRATOR;
          } catch {
            return false;
          }
        })()),
      // Send the raw bitmask as a STRING: it is a 64-bit value and a JS number
      // silently loses precision above 2^53. PostgREST coerces the string into
      // the bigint column exactly.
      permissions: (g.permissions ?? "0"),
      synced_at: nowIso,
    }));
    const { error: memberErr } = await admin
      .from("user_guilds")
      .upsert(membershipRows, { onConflict: "user_id,guild_id" });
    if (memberErr) {
      return errorResponse(500, "db_members_failed", memberErr.message);
    }
  }

  // Drop stale memberships: a user who lost access (or left) must not keep an
  // RLS-granting row. Delete this user's rows that are no longer in the list.
  const keepIds = manageable.map((g) => g.id);
  let deleteQuery = admin.from("user_guilds").delete().eq("user_id", userId);
  if (keepIds.length > 0) {
    deleteQuery = deleteQuery.not("guild_id", "in", `(${keepIds.join(",")})`);
  }
  const { error: pruneErr } = await deleteQuery;
  if (pruneErr) {
    // Non-fatal: the upsert above already succeeded. Report but continue.
    console.error("sync-guilds prune failed:", pruneErr.message);
  }

  // --------------------------------------------------------------------
  // 6. Fetch channels + roles, for the top few guilds only
  // --------------------------------------------------------------------
  // Discord rate-limits per-guild; fetching every guild's channels on each sync
  // would be slow and could trip 429s. We resolve the guilds the caller asked
  // about (all manageable ones, capped) and degrade to empty lists on failure —
  // the dashboard renders without them rather than showing wrong data.
  const CHANNEL_FETCH_LIMIT = 10;
  const guildsWithDetail = await Promise.all(
    manageable.slice(0, CHANNEL_FETCH_LIMIT).map(async (g) => {
      let channels: Array<{ id: string; name: string; type: number }> = [];
      let roles: Array<{ id: string; name: string; color: number }> = [];
      try {
        const [chRes, roleRes] = await Promise.all([
          fetchWithTimeout(
            `${DISCORD_API}/guilds/${g.id}/channels`,
            { headers: discordHeaders },
            DISCORD_TIMEOUT_MS,
          ),
          fetchWithTimeout(
            `${DISCORD_API}/guilds/${g.id}/roles`,
            { headers: discordHeaders },
            DISCORD_TIMEOUT_MS,
          ),
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
        // Leave the empty lists; the dashboard shows "no channels" rather than
        // inventing them.
      }
      return {
        id: g.id,
        name: g.name,
        icon_url: g.icon
          ? `https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png?size=128`
          : null,
        is_admin: Boolean(g.owner),
        permissions: g.permissions ?? "0",
        channels,
        roles,
      };
    }),
  );

  // --------------------------------------------------------------------
  // 7. Merge in bot_present from the database and return
  // --------------------------------------------------------------------
  const { data: botRows } = await admin
    .from("guilds")
    .select("id,bot_present")
    .in("id", manageable.map((g) => g.id));
  const botPresent = new Map<string, boolean>(
    (botRows ?? []).map((r) => [String(r.id), Boolean(r.bot_present)]),
  );

  return json({
    guilds: guildsWithDetail.map((g) => ({
      ...g,
      bot_present: botPresent.get(g.id) ?? false,
    })),
    synced_at: nowIso,
  });
});
