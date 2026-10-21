// ============================================================================
// Edge Function: delete-account
// ============================================================================
// Permanently deletes the caller's account and the data that belongs to them.
//
// WHY THIS EXISTS
//   Supabase removes the auth user only via the admin API, which needs the
//   service-role key. That key must never reach the browser, so deletion has to
//   happen here. This is also the "right to erasure" path promised in the
//   Privacy Policy.
//
// WHAT GETS DELETED
//   Deleting the auth user cascades through the schema's foreign keys:
//     - profiles        (on delete cascade)  -> the account <-> Discord id link
//     - user_guilds      (on delete cascade)  -> all guild memberships
//     - ai_interactions  (on delete cascade)  -> the user's own summaries
//     - user_preferences (on delete cascade)  -> the saved theme
//     - dev_users        (on delete cascade)  -> the dev allowlist row
//
//   Two tables do NOT key on auth.users, so no cascade reaches them. This
//   function removes their rows by hand, using the Discord snowflake it reads
//   from profiles first (after the auth user is gone, that mapping is gone too):
//     - bot_dms         the user's direct messages to the Bot
//     - bot_dm_outbox   replies the operator had queued to send to them
//   Direct messages are deleted rather than blanked, because a DM thread is
//   about one person and loses its meaning without them.
//
// WHAT SURVIVES, WITHOUT YOU
//   Two tables keep their rows for the server's continuity:
//     - ai_memory   the conversation text (a reply is uninterpretable without
//                   the prompt it answers), so the TEXT stays and the
//                   identifiers are cleared.
//     - bot_events  the server's audit trail, so the EVENT stays and the actor
//                   is cleared.
//   A Discord snowflake is a name tag, not an anonymous number: leaving it on a
//   row would make the word "anonymised" false, so this function clears
//   discord_user_id / user_display_name and actor_id / actor_name explicitly
//   BEFORE deleting the user. If any of these steps fails the function reports
//   the failure and deletes nothing, rather than quietly leaving the identifiers.
//
// AUTH
//   The caller must present their own valid Supabase user JWT. The function
//   verifies it with the auth server and deletes ONLY that user — a user can
//   never delete someone else, and the id is never read from the body.
//
// DEPLOY
//   supabase functions deploy delete-account
//
// The dashboard calls: {SUPABASE_URL}/functions/v1/delete-account
// with the user's access token in Authorization and an empty body.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function errorResponse(status: number, code: string, message: string): Response {
  return new Response(
    JSON.stringify({ error: { code, message } }),
    { status, headers: { ...CORS_HEADERS, "Content-Type": "application/json" } },
  );
}

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

  // Verify the caller. The user id comes from Supabase, never from the body,
  // so one user cannot delete another by passing a different id.
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
    // handled below
  }
  if (!userId) {
    return errorResponse(401, "invalid_auth", "Invalid or expired token.");
  }

  const admin = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  // --- strip identifiers that no cascade reaches ---------------------------
  // The row has to be read first: after the auth user is gone, the mapping from
  // account to Discord snowflake is gone with it.
  //
  // Read it as TEXT. A Discord snowflake is ~1e18, well past JS's 2^53 exact
  // integer range, so PostgREST's JSON number is already rounded by the time it
  // reaches us (…333 arrives as …400) and an `eq` filter built from that number
  // matches no rows at all - the update would silently do nothing while the
  // response still claimed the identifiers were cleared. Same rule as the
  // dashboard: cast every snowflake to text.
  const { data: profile, error: profileError } = await admin
    .from("profiles")
    .select("discord_user_id_text:discord_user_id::text")
    .eq("user_id", userId)
    .maybeSingle();

  if (profileError) {
    return errorResponse(500, "profile_lookup_failed", profileError.message);
  }

  const discordUserId: string | null = profile?.discord_user_id_text ?? null;
  if (discordUserId !== null) {
    const [memory, events, dms, outbox] = await Promise.all([
      admin
        .from("ai_memory")
        .update({ discord_user_id: null, user_display_name: null })
        .eq("discord_user_id", discordUserId),
      admin
        .from("bot_events")
        .update({ actor_id: null, actor_name: null })
        .eq("actor_id", discordUserId),
      admin
        .from("bot_dms")
        .delete()
        .eq("discord_user_id", discordUserId),
      admin
        .from("bot_dm_outbox")
        .delete()
        .eq("recipient_id", discordUserId),
    ]);
    const failure = memory.error ?? events.error ?? dms.error ?? outbox.error;
    if (failure) {
      // Refuse rather than half-fulfil: the response below promises the link is
      // gone, and a retry is cheap.
      return errorResponse(500, "anonymise_failed", failure.message);
    }
  }

  // Delete the auth user. The schema's foreign keys cascade the rest, so we do
  // not delete rows by hand (which could drift from the constraints).
  const { error } = await admin.auth.admin.deleteUser(userId);
  if (error) {
    return errorResponse(500, "delete_failed", error.message);
  }

  return new Response(
    JSON.stringify({
      deleted: true,
      // State plainly what happened, including the two things that are kept and
      // what was removed from them.
      note:
        "Your account, server memberships, AI interaction summaries, dashboard preferences, and your direct messages to the Bot were deleted. " +
        "Conversation memory kept by servers you chatted in stays as text, with your Discord id and display name cleared, " +
        "and activity log entries for those servers stay with the acting user cleared.",
    }),
    { status: 200, headers: { ...CORS_HEADERS, "Content-Type": "application/json" } },
  );
});
