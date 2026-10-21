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
//     - user_guilds      (on delete cascade)  -> all guild memberships
//     - ai_interactions  (on delete cascade)  -> the user's own summaries
//     - user_preferences (on delete cascade)  -> the saved theme
//     - dev_users        (on delete cascade)  -> the dev allowlist row
//   ai_memory.user_id is "on delete set null" BY DESIGN: the transcript stays
//   for the server's continuity but is anonymised. It is never linked back to
//   a person afterwards. The response states this so the promise is not vague.
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

  // Delete the auth user. The schema's foreign keys cascade the rest, so we do
  // not delete rows by hand (which could drift from the constraints).
  const { error } = await admin.auth.admin.deleteUser(userId);
  if (error) {
    return errorResponse(500, "delete_failed", error.message);
  }

  return new Response(
    JSON.stringify({
      deleted: true,
      // State plainly what happened, including the one thing that is retained.
      note:
        "Your account, guild memberships, AI interaction summaries and dashboard preferences were deleted. " +
        "Conversation memory kept by servers you chatted in is anonymised (no longer linked to you).",
    }),
    { status: 200, headers: { ...CORS_HEADERS, "Content-Type": "application/json" } },
  );
});
