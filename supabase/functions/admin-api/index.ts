// ============================================================================
// Edge Function: admin-api
// ============================================================================
// The ONE server-side path the owner admin dashboard talks to.
//
// WHY A FUNCTION AND NOT RLS
//   The admin dashboard reads every table (including other people's DMs and the
//   audit trail) and writes settings for servers the caller does not manage. The
//   user dashboard's model is "browser holds the anon key, RLS decides". That
//   model cannot express "owner may see everything" without opening every table
//   to a policy that a stolen JWT could also satisfy.
//
//   So the elevated reads happen HERE, with the service_role key, after a
//   server-side check that the caller is on the dev_users allowlist. The browser
//   never holds a service key, and the operator tables stay RLS deny-all.
//
// AUTHENTICATION vs AUTHORIZATION
//   Authn: the JWT is verified against GoTrue (/auth/v1/user) on every request.
//   Authz: the resulting user id must exist in public.dev_users. Both are
//          re-checked per request; nothing is cached in the browser.
//
// EVERY WRITE IS AUDITED
//   Mutating actions append to admin_audit (who / what / target) before
//   returning, so the dashboard has a truthful record of what the operator did.
//
// DEPLOY
//   supabase functions deploy admin-api --use-api
//   No extra secrets: SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY
//   are provided to every function by the platform.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// Columns the owner may write on guild_settings. Same allowlist as
// apply-guild-settings: it exists so a caller cannot rewrite guild_id/created_at
// or updated_at (which the optimistic lock depends on).
const EDITABLE_SETTINGS = new Set<string>([
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

// Tables the read-only explorer may touch, with a sensible default ordering.
// An allowlist, not "whatever PostgREST exposes": it keeps the explorer from
// wandering into a future table nobody meant to publish.
const TABLES: Record<string, { order?: string; asc?: boolean }> = {
  guilds: { order: "created_at", asc: false },
  guild_settings: { order: "created_at", asc: false },
  user_guilds: {},
  profiles: { order: "created_at", asc: false },
  reply_actions: { order: "sort_order", asc: true },
  guild_replies: { order: "updated_at", asc: false },
  qotd_questions: { order: "id", asc: true },
  ai_memory: { order: "created_at", asc: false },
  ai_interactions: { order: "created_at", asc: false },
  bot_events: { order: "created_at", asc: false },
  dev_users: { order: "created_at", asc: false },
  user_preferences: { order: "updated_at", asc: false },
  bot_dms: { order: "created_at", asc: false },
  bot_dm_outbox: { order: "created_at", asc: false },
  admin_audit: { order: "created_at", asc: false },
  admin_requests: { order: "created_at", asc: false },
};

const GUILD_SELECT =
  "id::text,name,icon_url,bot_present,member_count,online_count,stats_updated_at,created_at," +
  "guild_settings(ai_enabled,qotd_enabled,prefix,updated_at)";

// `*` plus the two bigint[] columns read a second time as text. PostgREST
// serialises bigint as a JSON number, and a Discord snowflake is past 2^53, so
// an id inside these arrays arrives rounded (…333 becomes …400). The form would
// then display the wrong channel and saving it would write the rounded value
// back, silently retargeting the AI. Postgres renders int8[] as "{123,456}",
// which parseBigintArrayText turns back into exact strings.
const SETTINGS_SELECT =
  "*,ai_channel_ids_text:ai_channel_ids::text," +
  "ai_memory_ignored_channels_text:ai_memory_ignored_channels::text";

function parseBigintArrayText(value: unknown): string[] | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  const body = text.startsWith("{") && text.endsWith("}") ? text.slice(1, -1) : text;
  if (body.trim() === "") return [];
  return body
    .split(",")
    .map((part) => part.trim().replace(/^"|"$/g, ""))
    .filter(Boolean);
}

/** Make a guild_settings row safe to send to a browser with 64-bit ids intact. */
function settingsForClient(data: Record<string, unknown>): Record<string, unknown> {
  const out = { ...data };
  if (typeof out.guild_id === "number" || typeof out.guild_id === "string") {
    out.guild_id = String(out.guild_id);
  }
  for (const column of BIGINT_ARRAY_COLUMNS.guild_settings) {
    const alias = `${column}_text`;
    if (alias in out) {
      out[column] = parseBigintArrayText(out[alias]);
      delete out[alias];
    }
  }
  return out;
}

// 64-bit columns, per table, for the read-only explorer. PostgREST renders
// bigint as a JSON number and the browser parses that as a double, so every
// Discord snowflake past 2^53 would arrive rounded - the explorer would show
// ids that do not exist. It therefore asks for each of these a second time as
// text and swaps it into the row before answering. schema.sql verification 08.13
// lists every bigint column in public; this map must cover all of them.
const BIGINT_COLUMNS: Record<string, string[]> = {
  guilds: ["id"],
  guild_settings: ["guild_id", "log_channel_id", "qotd_channel_id", "qotd_role_id"],
  user_guilds: ["guild_id", "permissions"],
  profiles: ["discord_user_id"],
  guild_replies: ["guild_id"],
  qotd_questions: ["id", "guild_id", "asked_message_id"],
  ai_memory: ["id", "guild_id", "channel_id", "discord_user_id"],
  ai_interactions: ["id", "guild_id", "channel_id", "discord_user_id"],
  bot_events: ["id", "guild_id", "actor_id", "channel_id"],
  bot_dms: ["id", "discord_user_id", "message_id"],
  bot_dm_outbox: ["id", "recipient_id"],
  admin_audit: ["id"],
  admin_requests: ["id"],
};

const BIGINT_ARRAY_COLUMNS: Record<string, string[]> = {
  guild_settings: ["ai_channel_ids", "ai_memory_ignored_channels"],
};

function explorerSelect(table: string): string {
  const casts = [
    ...(BIGINT_COLUMNS[table] ?? []),
    ...(BIGINT_ARRAY_COLUMNS[table] ?? []),
  ].map((column) => `${column}_text:${column}::text`);
  return casts.length ? `*,${casts.join(",")}` : "*";
}

function explorerRows(table: string, rows: Record<string, unknown>[]): Record<string, unknown>[] {
  const scalar = BIGINT_COLUMNS[table] ?? [];
  const arrays = BIGINT_ARRAY_COLUMNS[table] ?? [];
  if (scalar.length === 0 && arrays.length === 0) return rows;
  return rows.map((row) => {
    const out = { ...row };
    for (const column of scalar) {
      if (`${column}_text` in out) {
        out[column] = out[`${column}_text`];
        delete out[`${column}_text`];
      }
    }
    for (const column of arrays) {
      if (`${column}_text` in out) {
        out[column] = parseBigintArrayText(out[`${column}_text`]);
        delete out[`${column}_text`];
      }
    }
    return out;
  });
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

function clampInt(value: unknown, min: number, max: number, fallback: number): number {
  const n = typeof value === "number" ? value : Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(n)));
}

function asSnowflake(value: unknown): string | null {
  const raw = String(value ?? "").trim();
  return /^\d{5,25}$/.test(raw) ? raw : null;
}

/** Strip characters that would break a PostgREST `or`/`ilike` expression. */
function safeSearch(value: unknown): string | null {
  const raw = String(value ?? "").trim().replace(/[,()]/g, " ").slice(0, 80);
  return raw || null;
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS_HEADERS });
  if (req.method !== "POST") return errorResponse(405, "method_not_allowed", "Use POST.");

  const jwt = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
  if (!jwt) return errorResponse(401, "missing_auth", "Missing Authorization header.");

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !anonKey || !serviceKey) {
    return errorResponse(500, "server_misconfigured", "Supabase env missing.");
  }
  const base = supabaseUrl.replace(/\/+$/, "");

  // --- 1. Authenticate -----------------------------------------------------
  let userId: string | null = null;
  let email: string | null = null;
  try {
    const res = await fetch(`${base}/auth/v1/user`, {
      method: "GET",
      headers: { Authorization: `Bearer ${jwt}`, apikey: anonKey },
    });
    if (res.ok) {
      const user = await res.json();
      if (typeof user?.id === "string" && user.id) {
        userId = user.id;
        email = typeof user?.email === "string" ? user.email : null;
      }
    }
  } catch {
    // handled below
  }
  if (!userId) return errorResponse(401, "invalid_auth", "Invalid or expired token.");

  const service = createClient(base, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  // --- 2. Authorize against the owner allowlist ----------------------------
  const { data: devRow, error: devErr } = await service
    .from("dev_users")
    .select("user_id,note")
    .eq("user_id", userId)
    .maybeSingle();
  if (devErr) return errorResponse(500, "db_error", devErr.message);
  if (!devRow) {
    return errorResponse(403, "not_admin", "This account is not on the operator allowlist.");
  }

  const actorLabel = email ?? userId;
  const audit = async (action: string, target: string | null, detail: unknown) => {
    try {
      await service.from("admin_audit").insert({
        actor_id: userId,
        actor_label: actorLabel,
        action,
        target,
        detail: detail ?? {},
      });
    } catch (err) {
      // An audit failure must be loud but must not silently drop the action's
      // result the caller already received.
      console.error("admin_audit insert failed:", err instanceof Error ? err.message : err);
    }
  };

  // --- 3. Parse the request ------------------------------------------------
  let action = "";
  let params: Record<string, unknown> = {};
  try {
    const body = await req.json();
    action = typeof body?.action === "string" ? body.action : "";
    params = (body?.params && typeof body.params === "object" ? body.params : {}) as Record<string, unknown>;
  } catch {
    return errorResponse(400, "bad_json", "Request body must be valid JSON.");
  }
  if (!action) return errorResponse(400, "bad_action", "Missing action.");

  // --- 4. Dispatch ---------------------------------------------------------
  try {
    switch (action) {
      case "session":
        return json({ user_id: userId, email, is_admin: true });

      // ------------------------------------------------------------------
      case "overview": {
        const count = async (build: () => Promise<{ count: number | null; error: unknown }>) => {
          const { count: c, error } = await build();
          if (error) throw new Error((error as { message?: string }).message ?? "count failed");
          return c ?? 0;
        };
        const since24 = new Date(Date.now() - 86_400_000).toISOString();
        const since7 = new Date(Date.now() - 7 * 86_400_000).toISOString();

        const [guildsRes, events24, errors24, ai24, dms24, profiles, devs, outbox, requests, recent, aiRows] =
          await Promise.all([
            service.from("guilds").select(GUILD_SELECT),
            count(() =>
              service.from("bot_events").select("*", { count: "exact", head: true }).gte("created_at", since24)
            ),
            count(() =>
              service.from("bot_events").select("*", { count: "exact", head: true })
                .gte("created_at", since24).eq("level", "error")
            ),
            count(() =>
              service.from("ai_interactions").select("*", { count: "exact", head: true }).gte("created_at", since24)
            ),
            count(() =>
              service.from("bot_dms").select("*", { count: "exact", head: true }).gte("created_at", since24)
            ),
            count(() => service.from("profiles").select("*", { count: "exact", head: true })),
            count(() => service.from("dev_users").select("*", { count: "exact", head: true })),
            count(() =>
              service.from("bot_dm_outbox").select("*", { count: "exact", head: true }).eq("status", "pending")
            ),
            count(() =>
              service.from("admin_requests").select("*", { count: "exact", head: true }).eq("status", "pending")
            ),
            service.from("bot_events")
              .select("id,guild_id::text,category,title,level,created_at")
              .order("created_at", { ascending: false })
              .limit(8),
            service.from("ai_interactions")
              .select("model,succeeded,latency_ms,prompt_tokens,completion_tokens,error_kind")
              .gte("created_at", since7)
              .limit(50_000),
          ]);
        const ai7 = await count(() =>
          service.from("ai_interactions").select("*", { count: "exact", head: true }).gte("created_at", since7)
        );

        if (guildsRes.error) return errorResponse(500, "db_error", guildsRes.error.message);
        const guilds = (guildsRes.data ?? []) as Record<string, unknown>[];
        const present = guilds.filter((g) => g.bot_present);
        const members = present.reduce((sum, g) => sum + (Number(g.member_count) || 0), 0);
        const online = present.reduce((sum, g) => sum + (Number(g.online_count) || 0), 0);

        const rows = (aiRows.data ?? []) as Record<string, unknown>[];
        const ok7 = rows.filter((r) => r.succeeded).length;
        const lat = rows.map((r) => Number(r.latency_ms)).filter((n) => Number.isFinite(n) && n > 0);
        const byModel: Record<string, number> = {};
        for (const r of rows) {
          const m = String(r.model ?? "unknown");
          byModel[m] = (byModel[m] ?? 0) + 1;
        }

        return json({
          guilds: { total: guilds.length, present: present.length },
          members: { total: members, online },
          events: { last24h: events24, errors24h: errors24 },
          ai: {
            last24h: ai24,
            last7d: ai7,
            sampled7d: rows.length,
            successRate7d: rows.length ? Math.round((ok7 / rows.length) * 1000) / 10 : null,
            avgLatencyMs7d: lat.length ? Math.round(lat.reduce((a, b) => a + b, 0) / lat.length) : null,
            byModel,
          },
          dms: { last24h: dms24, outboxPending: outbox },
          users: { registered: profiles, admins: devs },
          requestsPending: requests,
          recentEvents: recent.data ?? [],
        });
      }

      // ------------------------------------------------------------------
      case "guilds.list": {
        const { data, error } = await service.from("guilds").select(GUILD_SELECT).order("name", { ascending: true });
        if (error) return errorResponse(500, "db_error", error.message);
        return json({ guilds: data ?? [] });
      }

      case "guilds.detail": {
        const gid = asSnowflake(params.guild_id);
        if (!gid) return errorResponse(400, "bad_guild_id", "guild_id must be a snowflake.");
        const [guild, settings, events, ai, memory, replies, qotd] = await Promise.all([
          service.from("guilds").select(GUILD_SELECT).eq("id", gid).maybeSingle(),
          service.from("guild_settings").select(SETTINGS_SELECT).eq("guild_id", gid).maybeSingle(),
          service.from("bot_events").select("*", { count: "exact", head: true }).eq("guild_id", gid),
          service.from("ai_interactions").select("*", { count: "exact", head: true }).eq("guild_id", gid),
          service.from("ai_memory").select("*", { count: "exact", head: true }).eq("guild_id", gid),
          service.from("guild_replies").select("*", { count: "exact", head: true }).eq("guild_id", gid),
          service.from("qotd_questions").select("*", { count: "exact", head: true }).eq("guild_id", gid),
        ]);
        if (guild.error) return errorResponse(500, "db_error", guild.error.message);
        if (settings.error) return errorResponse(500, "db_error", settings.error.message);
        const settingsRow = settings.data
          ? settingsForClient(settings.data as Record<string, unknown>)
          : null;
        return json({
          guild: guild.data,
          settings: settingsRow,
          counts: {
            events: events.count ?? 0,
            ai: ai.count ?? 0,
            memory: memory.count ?? 0,
            replies: replies.count ?? 0,
            qotd: qotd.count ?? 0,
          },
        });
      }

      case "guilds.update": {
        const gid = asSnowflake(params.guild_id);
        if (!gid) return errorResponse(400, "bad_guild_id", "guild_id must be a snowflake.");
        const changes = (params.changes && typeof params.changes === "object" ? params.changes : {}) as Record<string, unknown>;
        const payload: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(changes)) if (EDITABLE_SETTINGS.has(k)) payload[k] = v;
        if (Object.keys(payload).length === 0) return errorResponse(400, "no_changes", "No editable columns supplied.");

        const { data, error } = await service
          .from("guild_settings")
          .update(payload)
          .eq("guild_id", gid)
          .select(SETTINGS_SELECT)
          .maybeSingle();
        if (error) {
          const status = error.code === "23514" || error.code === "22001" || error.code === "22003" ? 400 : 500;
          return errorResponse(status, error.code ?? "db_error", error.message);
        }
        if (!data) return errorResponse(404, "settings_missing", "No settings row for that server yet.");
        await audit("guilds.update", gid, { columns: Object.keys(payload) });
        return json({ settings: settingsForClient(data as Record<string, unknown>) });
      }

      // ------------------------------------------------------------------
      case "events.list": {
        const limit = clampInt(params.limit, 1, 500, 100);
        let q = service
          .from("bot_events")
          .select(
            "id,guild_id::text,category,title,description,actor_id::text,actor_name,channel_id::text,level,metadata,created_at",
            { count: "exact" },
          )
          .order("created_at", { ascending: false })
          .limit(limit);
        const gid = asSnowflake(params.guild_id);
        if (gid) q = q.eq("guild_id", gid);
        if (params.category) q = q.eq("category", String(params.category));
        if (params.level) q = q.eq("level", String(params.level));
        const search = safeSearch(params.search);
        if (search) q = q.or(`title.ilike.%${search}%,description.ilike.%${search}%`);
        const { data, error, count } = await q;
        if (error) return errorResponse(500, "db_error", error.message);
        return json({ events: data ?? [], total: count ?? null });
      }

      // ------------------------------------------------------------------
      case "ai.stats": {
        const days = clampInt(params.days, 1, 365, 7);
        const since = new Date(Date.now() - days * 86_400_000).toISOString();
        let q = service
          .from("ai_interactions")
          .select("guild_id::text,model,succeeded,prompt_tokens,completion_tokens,latency_ms,error_kind,created_at", {
            count: "exact",
          })
          .gte("created_at", since)
          .order("created_at", { ascending: false })
          .limit(50_000);
        const gid = asSnowflake(params.guild_id);
        if (gid) q = q.eq("guild_id", gid);
        const { data, error, count } = await q;
        if (error) return errorResponse(500, "db_error", error.message);

        const rows = (data ?? []) as Record<string, unknown>[];
        const succeeded = rows.filter((r) => r.succeeded).length;
        const lat = rows.map((r) => Number(r.latency_ms)).filter((n) => Number.isFinite(n) && n > 0);
        const prompt = rows.reduce((s, r) => s + (Number(r.prompt_tokens) || 0), 0);
        const completion = rows.reduce((s, r) => s + (Number(r.completion_tokens) || 0), 0);
        const byModel: Record<string, number> = {};
        const byDay: Record<string, number> = {};
        const errors: Record<string, number> = {};
        for (const r of rows) {
          const m = String(r.model ?? "unknown");
          byModel[m] = (byModel[m] ?? 0) + 1;
          const day = String(r.created_at).slice(0, 10);
          byDay[day] = (byDay[day] ?? 0) + 1;
          if (!r.succeeded) {
            const k = String(r.error_kind ?? "unknown");
            errors[k] = (errors[k] ?? 0) + 1;
          }
        }
        return json({
          window_days: days,
          total: count ?? rows.length,
          sampled: rows.length,
          succeeded,
          failed: rows.length - succeeded,
          avg_latency_ms: lat.length ? Math.round(lat.reduce((a, b) => a + b, 0) / lat.length) : null,
          prompt_tokens: prompt,
          completion_tokens: completion,
          by_model: byModel,
          by_day: byDay,
          errors,
        });
      }

      case "ai.memory": {
        const limit = clampInt(params.limit, 1, 200, 50);
        const offset = clampInt(params.offset, 0, 1_000_000, 0);
        let q = service
          .from("ai_memory")
          .select(
            "id,guild_id::text,channel_id::text,user_id::text,discord_user_id::text,user_display_name,user_content,assistant_content,created_at",
            { count: "exact" },
          )
          .order("created_at", { ascending: false })
          .range(offset, offset + limit - 1);
        const gid = asSnowflake(params.guild_id);
        if (gid) q = q.eq("guild_id", gid);
        const uid = String(params.user_id ?? "").trim();
        if (/^[0-9a-f-]{36}$/i.test(uid)) q = q.eq("user_id", uid);
        const { data, error, count } = await q;
        if (error) return errorResponse(500, "db_error", error.message);
        // NOTE: this action returns real message text. It exists because the
        // operator chose to allow it; the Privacy Policy discloses it.
        await audit("ai.memory.read", gid, { offset, limit });
        return json({ memory: data ?? [], total: count ?? null });
      }

      case "ai.prune": {
        const gid = asSnowflake(params.guild_id);
        const targetGuilds = gid
          ? [{ guild_id: gid }]
          : ((await service.from("guild_settings").select("guild_id::text,ai_memory_days")).data ?? []);
        let deleted = 0;
        for (const g of targetGuilds as Record<string, unknown>[]) {
          const days = Number(g.ai_memory_days ?? 30);
          if (!Number.isFinite(days) || days <= 0) continue; // 0 = keep forever
          const cutoff = new Date(Date.now() - days * 86_400_000).toISOString();
          const { error, count } = await service
            .from("ai_memory")
            .delete({ count: "exact" })
            .eq("guild_id", String(g.guild_id))
            .lt("created_at", cutoff);
          if (error) return errorResponse(500, "db_error", error.message);
          deleted += count ?? 0;
        }
        await audit("ai.prune", gid, { deleted });
        return json({ deleted });
      }

      case "ai.purge": {
        const gid = asSnowflake(params.guild_id);
        if (!gid) return errorResponse(400, "bad_guild_id", "guild_id is required for a purge.");
        const { error, count } = await service.from("ai_memory").delete({ count: "exact" }).eq("guild_id", gid);
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("ai.purge", gid, { deleted: count ?? 0 });
        return json({ deleted: count ?? 0 });
      }

      // ------------------------------------------------------------------
      case "replies.list": {
        const gid = asSnowflake(params.guild_id);
        if (!gid) return errorResponse(400, "bad_guild_id", "guild_id is required.");
        const [catalog, overrides] = await Promise.all([
          service.from("reply_actions").select("key,label,emoji,default_templates,sort_order").order("sort_order"),
          service.from("guild_replies").select("action,templates,enabled,updated_at").eq("guild_id", gid),
        ]);
        if (catalog.error) return errorResponse(500, "db_error", catalog.error.message);
        return json({ catalog: catalog.data ?? [], overrides: overrides.data ?? [] });
      }

      case "replies.update": {
        const gid = asSnowflake(params.guild_id);
        const actionKey = String(params.action ?? "").trim();
        if (!gid || !actionKey) return errorResponse(400, "bad_request", "guild_id and action are required.");
        const templates = Array.isArray(params.templates)
          ? (params.templates as unknown[]).map((t) => String(t)).filter((t) => t.trim()).slice(0, 25)
          : null;
        if (!templates) return errorResponse(400, "bad_templates", "templates must be an array.");
        const enabled = params.enabled === undefined ? true : Boolean(params.enabled);
        const { data, error } = await service
          .from("guild_replies")
          .upsert(
            { guild_id: gid, action: actionKey, templates, enabled, updated_at: new Date().toISOString() },
            { onConflict: "guild_id,action" },
          )
          .select("*")
          .maybeSingle();
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("replies.update", gid, { action: actionKey, templates: templates.length, enabled });
        return json({ reply: data });
      }

      // ------------------------------------------------------------------
      case "qotd.list": {
        const gid = asSnowflake(params.guild_id);
        if (!gid) return errorResponse(400, "bad_guild_id", "guild_id is required.");
        const { data, error } = await service
          .from("qotd_questions")
          .select("id,guild_id::text,question,position,created_at,asked_at,asked_message_id::text")
          .eq("guild_id", gid)
          .order("asked_at", { ascending: true, nullsFirst: true })
          .order("position", { ascending: true });
        if (error) return errorResponse(500, "db_error", error.message);
        return json({ questions: data ?? [] });
      }

      case "qotd.add": {
        const gid = asSnowflake(params.guild_id);
        const question = String(params.question ?? "").trim();
        if (!gid || !question) return errorResponse(400, "bad_request", "guild_id and question are required.");
        const { data: last } = await service
          .from("qotd_questions")
          .select("position")
          .eq("guild_id", gid)
          .order("position", { ascending: false })
          .limit(1)
          .maybeSingle();
        const position = (Number(last?.position) || 0) + 1;
        const { data, error } = await service
          .from("qotd_questions")
          .insert({ guild_id: gid, question: question.slice(0, 2000), position, created_by: userId })
          .select("id,question,position,created_at,asked_at")
          .maybeSingle();
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("qotd.add", gid, { id: data?.id });
        return json({ question: data });
      }

      case "qotd.delete": {
        const id = clampInt(params.id, 1, Number.MAX_SAFE_INTEGER, 0);
        if (!id) return errorResponse(400, "bad_id", "id is required.");
        const { error } = await service.from("qotd_questions").delete().eq("id", id);
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("qotd.delete", String(id), {});
        return json({ deleted: true });
      }

      case "qotd.reorder": {
        const gid = asSnowflake(params.guild_id);
        const ids = Array.isArray(params.ids) ? (params.ids as unknown[]).map((v) => clampInt(v, 1, Number.MAX_SAFE_INTEGER, 0)).filter(Boolean) : [];
        if (!gid || ids.length === 0) return errorResponse(400, "bad_request", "guild_id and ids are required.");
        for (let i = 0; i < ids.length; i++) {
          const { error } = await service
            .from("qotd_questions")
            .update({ position: i + 1 })
            .eq("id", ids[i])
            .eq("guild_id", gid);
          if (error) return errorResponse(500, "db_error", error.message);
        }
        await audit("qotd.reorder", gid, { count: ids.length });
        return json({ reordered: ids.length });
      }

      // ------------------------------------------------------------------
      case "users.list": {
        const [profiles, devs, prefs] = await Promise.all([
          service.from("profiles").select("user_id,discord_user_id::text,created_at,updated_at").order("created_at", { ascending: false }),
          service.from("dev_users").select("user_id,note,created_at"),
          service.from("user_preferences").select("user_id,theme,updated_at"),
        ]);
        if (profiles.error) return errorResponse(500, "db_error", profiles.error.message);

        const authUsers: Record<string, { email: string | null; last_sign_in_at: string | null; created_at: string | null }> = {};
        try {
          const { data } = await service.auth.admin.listUsers({ page: 1, perPage: 1000 });
          for (const u of data?.users ?? []) {
            authUsers[u.id] = {
              email: u.email ?? null,
              last_sign_in_at: u.last_sign_in_at ?? null,
              created_at: u.created_at ?? null,
            };
          }
        } catch {
          // Auth listing is best-effort; the profile rows still stand alone.
        }

        const devSet = new Set((devs.data ?? []).map((d) => String(d.user_id)));
        const prefMap = new Map((prefs.data ?? []).map((p) => [String(p.user_id), String(p.theme)]));
        const users = (profiles.data ?? []).map((p) => {
          const id = String(p.user_id);
          return {
            user_id: id,
            discord_user_id: p.discord_user_id ?? null,
            email: authUsers[id]?.email ?? null,
            last_sign_in_at: authUsers[id]?.last_sign_in_at ?? null,
            profile_created_at: p.created_at,
            theme: prefMap.get(id) ?? null,
            is_admin: devSet.has(id),
          };
        });
        // Admins who signed in but have no profile row yet still deserve a line.
        for (const d of devs.data ?? []) {
          const id = String(d.user_id);
          if (!users.some((u) => u.user_id === id)) {
            users.unshift({
              user_id: id,
              discord_user_id: null,
              email: authUsers[id]?.email ?? null,
              last_sign_in_at: authUsers[id]?.last_sign_in_at ?? null,
              profile_created_at: null,
              theme: null,
              is_admin: true,
            });
          }
        }
        return json({ users, auth_listed: Object.keys(authUsers).length });
      }

      case "dev.add": {
        let targetId: string | null = null;
        const rawId = String(params.user_id ?? "").trim();
        if (/^[0-9a-f-]{36}$/i.test(rawId)) {
          targetId = rawId;
        } else {
          const wanted = String(params.email ?? "").trim().toLowerCase();
          if (!wanted) return errorResponse(400, "bad_request", "Provide a user_id or an email.");
          const { data } = await service.auth.admin.listUsers({ page: 1, perPage: 1000 });
          const match = (data?.users ?? []).find((u) => (u.email ?? "").toLowerCase() === wanted);
          if (!match) return errorResponse(404, "user_not_found", `No account exists for ${wanted}. They must sign in once first.`);
          targetId = match.id;
        }
        const { error } = await service
          .from("dev_users")
          .upsert({ user_id: targetId, note: String(params.note ?? "admin") }, { onConflict: "user_id" });
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("dev.add", targetId, {});
        return json({ added: targetId });
      }

      case "dev.remove": {
        const targetId = String(params.user_id ?? "").trim();
        if (!/^[0-9a-f-]{36}$/i.test(targetId)) return errorResponse(400, "bad_user_id", "user_id is required.");
        if (targetId === userId) return errorResponse(400, "self_removal", "You cannot remove your own admin access.");
        const { error } = await service.from("dev_users").delete().eq("user_id", targetId);
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("dev.remove", targetId, {});
        return json({ removed: targetId });
      }

      // ------------------------------------------------------------------
      case "dms.threads": {
        const { data, error, count } = await service
          .from("bot_dms")
          .select("discord_user_id::text,username,display_name,content,created_at", { count: "exact" })
          .order("created_at", { ascending: false })
          .limit(3000);
        if (error) return errorResponse(500, "db_error", error.message);
        const threads = new Map<string, { discord_user_id: string; username: string | null; display_name: string | null; last_message: string; last_at: string; sampled_count: number }>();
        for (const r of (data ?? []) as Record<string, unknown>[]) {
          const id = String(r.discord_user_id);
          const existing = threads.get(id);
          if (!existing) {
            threads.set(id, {
              discord_user_id: id,
              username: (r.username as string) ?? null,
              display_name: (r.display_name as string) ?? null,
              last_message: String(r.content ?? "").slice(0, 200),
              last_at: String(r.created_at),
              sampled_count: 1,
            });
          } else {
            existing.sampled_count += 1;
          }
        }
        // Reading DM content is a privacy-sensitive act: record it like every
        // other operator action (PRIVACY_POLICY: "every such read is recorded").
        await audit("dms.threads.read", null, {
          threads: threads.size,
          sampled: (data ?? []).length,
        });
        return json({ threads: [...threads.values()], total_messages: count ?? null, sampled: (data ?? []).length });
      }
      case "dms.messages": {
        const uid = asSnowflake(params.discord_user_id);
        if (!uid) return errorResponse(400, "bad_user_id", "discord_user_id must be a snowflake.");
        const limit = clampInt(params.limit, 1, 500, 200);
        // Newest first + reverse: a long thread must show its LATEST turns, not
        // its first 200. The counts let the UI admit when it is showing a slice.
        const { data, error, count } = await service
          .from("bot_dms")
          .select("id,discord_user_id::text,username,display_name,content,attachments,created_at", {
            count: "exact",
          })
          .eq("discord_user_id", uid)
          .order("created_at", { ascending: false })
          .limit(limit);
        if (error) return errorResponse(500, "db_error", error.message);
        const { data: outgoing, count: outgoingCount } = await service
          .from("bot_dm_outbox")
          .select("id,recipient_id::text,content,status,error,created_at,sent_at", { count: "exact" })
          .eq("recipient_id", uid)
          .order("created_at", { ascending: false })
          .limit(limit);
        await audit("dms.messages.read", uid, {
          limit,
          incoming_total: count ?? 0,
          outgoing_total: outgoingCount ?? 0,
        });
        return json({
          incoming: (data ?? []).slice().reverse(),
          outgoing: (outgoing ?? []).slice().reverse(),
          incoming_total: count ?? null,
          outgoing_total: outgoingCount ?? null,
        });
      }

      case "dms.send": {
        const uid = asSnowflake(params.recipient_id);
        const content = String(params.content ?? "").trim();
        if (!uid || !content) return errorResponse(400, "bad_request", "recipient_id and content are required.");
        const { data, error } = await service
          .from("bot_dm_outbox")
          .insert({ recipient_id: uid, content: content.slice(0, 4000), requested_by: userId })
          .select("id,status,created_at")
          .maybeSingle();
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("dms.send", uid, { length: content.length });
        return json({ queued: data });
      }

      // ------------------------------------------------------------------
      case "tables.list": {
        const rows = await Promise.all(
          Object.keys(TABLES).map(async (table) => {
            const { count } = await service.from(table).select("*", { count: "exact", head: true });
            return { table, rows: count ?? null };
          }),
        );
        return json({ tables: rows.sort((a, b) => a.table.localeCompare(b.table)) });
      }

      case "tables.rows": {
        const table = String(params.table ?? "");
        const meta = TABLES[table];
        if (!meta) return errorResponse(400, "bad_table", "That table is not in the explorer allowlist.");
        const limit = clampInt(params.limit, 1, 200, 50);
        const offset = clampInt(params.offset, 0, 1_000_000, 0);
        let q = service
          .from(table)
          .select(explorerSelect(table), { count: "exact" })
          .range(offset, offset + limit - 1);
        if (meta.order) q = q.order(meta.order, { ascending: meta.asc ?? false });
        const { data, error, count } = await q;
        if (error) return errorResponse(500, "db_error", error.message);
        return json({
          table,
          rows: explorerRows(table, (data ?? []) as Record<string, unknown>[]),
          total: count ?? null,
        });
      }

      // ------------------------------------------------------------------
      case "audit.list": {
        const limit = clampInt(params.limit, 1, 500, 100);
        const { data, error } = await service
          .from("admin_audit")
          .select("id,actor_id::text,actor_label,action,target,detail,created_at")
          .order("created_at", { ascending: false })
          .limit(limit);
        if (error) return errorResponse(500, "db_error", error.message);
        return json({ audit: data ?? [] });
      }

      case "requests.list": {
        const limit = clampInt(params.limit, 1, 200, 50);
        const { data, error } = await service
          .from("admin_requests")
          .select("id,kind,status,result,requested_by::text,created_at,processed_at")
          .order("created_at", { ascending: false })
          .limit(limit);
        if (error) return errorResponse(500, "db_error", error.message);
        return json({ requests: data ?? [] });
      }

      case "requests.resync": {
        const { data, error } = await service
          .from("admin_requests")
          .insert({ kind: "resync_commands", requested_by: userId })
          .select("id,kind,status,created_at")
          .maybeSingle();
        if (error) return errorResponse(500, "db_error", error.message);
        await audit("requests.resync", null, {});
        return json({ request: data });
      }

      default:
        return errorResponse(400, "unknown_action", `Unknown action: ${action}`);
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unexpected error.";
    console.error("admin-api failed:", action, message);
    return errorResponse(500, "server_error", message);
  }
});
