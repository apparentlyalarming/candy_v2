// ============================================================================
// Shared: verify a Discord user's permissions right now
// ============================================================================
// The dashboard used to trust `user_guilds`, which was written by the *user's*
// own browser-triggered sync. A demoted admin could simply never trigger that
// sync and keep write access indefinitely, including through PostgREST with a
// stolen JWT. This module asks Discord, using the BOT token, at the moment of
// the request. There is no cache to keep alive and nothing the caller controls.
//
// WHY THE BOT TOKEN AND NOT THE USER'S OAUTH TOKEN
//   The user token is issued at login, expires after a couple of hours, and is
//   exactly the thing a demoted user can choose not to refresh. The bot token is
//   held server-side and its answer reflects the guild's current state.
//
// NOTE ON COMPUTED PERMISSIONS
//   GET /guilds/{id}/members/{id} returns role IDs, NOT a permission bitmask.
//   Discord only computes effective permissions for the OAuth `@me/guilds` view.
//   So we fold @everyone + the member's roles ourselves.
// ============================================================================

export const DISCORD_API = "https://discord.com/api/v10";

/** Discord permission bits we care about. */
export const ADMINISTRATOR = 0x8n;
export const MANAGE_GUILD = 0x20n;

export const DISCORD_TIMEOUT_MS = 10_000;

/**
 * Discord rejects bot requests without a User-Agent (403), even with a valid
 * token. The URL/version are not used for anything but identification.
 */
const BOT_HEADERS = (token: string): Record<string, string> => ({
  Authorization: `Bot ${token}`,
  "User-Agent": "DiscordBot (https://candy-web-beryl.vercel.app, 1.0)",
});

export interface DiscordGuildSummary {
  id: string;
  name: string;
  icon: string | null;
}

export interface VerifyResult {
  /** Caller may manage this guild's settings right now. */
  ok: boolean;
  isOwner: boolean;
  /** Raw computed bitmask as a decimal string (bigint-safe for PostgREST). */
  permissions: string;
  /** owner or ADMINISTRATOR; mirrors the is_admin column. */
  isAdmin: boolean;
  guild: DiscordGuildSummary | null;
  /** Machine-readable reason when ok is false. */
  reason?: "not_member" | "no_permission" | "guild_unknown" | "discord_error";
}

interface DiscordRole {
  id: string;
  permissions: string;
}

interface DiscordMember {
  roles: string[];
}

function bigintOrZero(value: unknown): bigint {
  try {
    return BigInt(String(value ?? "0"));
  } catch {
    return 0n;
  }
}

/** fetch with a hard timeout so a hung Discord call cannot pin the worker. */
export async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number = DISCORD_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Resolve the caller's Discord snowflake from their Supabase JWT.
 *
 * We ask Supabase's auth server (it validates the token signature) rather than
 * decoding the JWT ourselves. `identities` is included in that response, so no
 * provider_token is needed: the Discord identity is already linked to the user.
 */
export async function getDiscordUserId(
  jwt: string,
  supabaseUrl: string,
  anonKey: string,
): Promise<string | null> {
  try {
    const res = await fetchWithTimeout(
      `${supabaseUrl.replace(/\/+$/, "")}/auth/v1/user`,
      { method: "GET", headers: { Authorization: `Bearer ${jwt}`, apikey: anonKey } },
      8_000,
    );
    if (!res.ok) return null;
    const user = await res.json();
    const identities: Array<Record<string, unknown>> = Array.isArray(user?.identities)
      ? user.identities
      : [];
    for (const identity of identities) {
      if (identity?.provider !== "discord") continue;
      const providerId = identity?.provider_id;
      if (typeof providerId === "string" && providerId) return providerId;
      const data = identity?.identity_data as Record<string, unknown> | undefined;
      const sub = data?.sub ?? data?.id;
      if (typeof sub === "string" && sub) return sub;
    }
  } catch {
    return null;
  }
  return null;
}

/** Every guild the bot is in. The dashboard only ever shows these. */
export async function listBotGuilds(botToken: string): Promise<DiscordGuildSummary[]> {
  const res = await fetchWithTimeout(`${DISCORD_API}/users/@me/guilds`, {
    headers: BOT_HEADERS(botToken),
  });
  if (!res.ok) throw new Error(`discord_guilds_${res.status}`);
  const raw = await res.json();
  if (!Array.isArray(raw)) return [];
  return raw.map((g: Record<string, unknown>) => ({
    id: String(g.id),
    name: String(g.name ?? ""),
    icon: (g.icon as string | null) ?? null,
  }));
}

/**
 * Ask Discord whether `discordUserId` may manage `guildId` right now.
 *
 * Three calls: guild (owner + identity), member (404 means not in the guild),
 * roles (to fold permission bits). Returns ok:false rather than throwing for
 * the expected "they're not allowed" cases, so callers can distinguish a denial
 * from an infrastructure failure.
 */
export async function verifyGuildAdmin(
  botToken: string,
  guildId: string,
  discordUserId: string,
): Promise<VerifyResult> {
  const headers = BOT_HEADERS(botToken);

  const guildRes = await fetchWithTimeout(`${DISCORD_API}/guilds/${guildId}`, { headers });
  // 403 and 404 both mean the bot cannot see this guild — usually because it is
  // not a member. Treat them the same so the caller gets a clear answer.
  if (guildRes.status === 404 || guildRes.status === 403) {
    return {
      ok: false,
      isOwner: false,
      permissions: "0",
      isAdmin: false,
      guild: null,
      reason: "guild_unknown",
    };
  }
  if (!guildRes.ok) {
    return {
      ok: false,
      isOwner: false,
      permissions: "0",
      isAdmin: false,
      guild: null,
      reason: "discord_error",
    };
  }
  const guild = await guildRes.json();
  const guildSummary: DiscordGuildSummary = {
    id: String(guild.id),
    name: String(guild.name ?? ""),
    icon: (guild.icon as string | null) ?? null,
  };
  const isOwner = String(guild.owner_id ?? "") === String(discordUserId);

  const memberRes = await fetchWithTimeout(
    `${DISCORD_API}/guilds/${guildId}/members/${discordUserId}`,
    { headers },
  );
  if (memberRes.status === 404) {
    return {
      ok: false,
      isOwner,
      permissions: "0",
      isAdmin: isOwner,
      guild: guildSummary,
      reason: "not_member",
    };
  }
  if (!memberRes.ok) {
    return {
      ok: false,
      isOwner,
      permissions: "0",
      isAdmin: isOwner,
      guild: guildSummary,
      reason: "discord_error",
    };
  }
  const member: DiscordMember = await memberRes.json();

  const rolesRes = await fetchWithTimeout(`${DISCORD_API}/guilds/${guildId}/roles`, { headers });
  if (!rolesRes.ok) {
    return {
      ok: false,
      isOwner,
      permissions: "0",
      isAdmin: isOwner,
      guild: guildSummary,
      reason: "discord_error",
    };
  }
  const roles: DiscordRole[] = await rolesRes.json();

  // @everyone's role id is the guild id. Its permissions apply to every member.
  let permissions = 0n;
  for (const role of roles) {
    if (role.id !== String(guildId)) continue;
    permissions |= bigintOrZero(role.permissions);
    break;
  }
  const memberRoleIds = new Set((member.roles ?? []).map(String));
  for (const role of roles) {
    if (!memberRoleIds.has(role.id)) continue;
    permissions |= bigintOrZero(role.permissions);
  }

  // ADMINISTRATOR implies every other permission, including MANAGE_GUILD. Set
  // the bit so the stored mask agrees with what Discord means, otherwise the
  // dashboard's bit test would say "admin but cannot manage" and confuse RLS.
  const hasAdmin = (permissions & ADMINISTRATOR) === ADMINISTRATOR;
  if (hasAdmin) permissions |= MANAGE_GUILD;

  const hasManage = (permissions & MANAGE_GUILD) === MANAGE_GUILD;
  const ok = isOwner || hasAdmin || hasManage;

  return {
    ok,
    isOwner,
    permissions: permissions.toString(),
    isAdmin: isOwner || hasAdmin,
    guild: guildSummary,
    reason: ok ? undefined : "no_permission",
  };
}
