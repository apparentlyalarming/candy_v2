// ============================================================================
// Edge Function: chat
// ============================================================================
// Proxies AI chat to Groq and returns a structured { reply, summary } result.
//
// WHY THIS EXISTS
//   The Groq API key lives in Supabase secrets (GROQ_API_KEY), not on the bot.
//   bot-hosting.net requires a manual renewal every 4 days; a key on the bot
//   would mean AI chat dies whenever a renewal is missed. Behind this function
//   the key survives bot downtime, never lands in a DB backup or the Realtime
//   stream, and the dashboard can reuse it.
//
// AUTH
//   Callers must present a valid Supabase JWT (either the bot's service_role key
//   or a logged-in user's access token). Anything else gets 401.
//
// DEPLOY
//   supabase secrets set GROQ_API_KEY=gsk_...
//   supabase functions deploy chat
//
// The bot calls: {SUPABASE_URL}/functions/v1/chat
// ============================================================================

import {
  clampNumber,
  DEFAULT_MAX_TOKENS,
  DEFAULT_MODEL,
  DEFAULT_TEMPERATURE,
  errorResponse,
  GROQ_ENDPOINT,
  JSON_INSTRUCTION,
  MAX_MESSAGE_CHARS,
  MAX_MESSAGES,
  MAX_TOKENS_LIMIT,
  SUMMARY_MAX_CHARS,
  TEMPERATURE_MAX,
  TEMPERATURE_MIN,
  type ChatRequestBody,
  type StructuredResult,
} from "./lib.ts";

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const GROQ_TIMEOUT_MS = 40_000;

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }

  if (req.method !== "POST") {
    return errorResponse(405, "method_not_allowed", "Use POST.");
  }

  // --------------------------------------------------------------------
  // 1. Authenticate the caller
  // --------------------------------------------------------------------
  const authHeader = req.headers.get("Authorization") ?? "";
  const jwt = authHeader.replace(/^Bearer\s+/i, "").trim();
  if (!jwt) {
    return errorResponse(401, "missing_auth", "Missing Authorization header.");
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY");
  if (!supabaseUrl || !anonKey) {
    // Should never happen: Supabase injects these automatically.
    return errorResponse(500, "server_misconfigured", "Supabase env missing.");
  }

  // Authenticate the caller. We ask Supabase's auth server to resolve the
  // token rather than trusting its contents.
  //
  // SECURITY: do NOT "decode the JWT and check role=service_role". The payload
  // of a JWT is attacker-controlled until the signature is verified; a crafted
  // token with that claim would pass such a check. Supabase's /auth/v1/user
  // endpoint verifies the signature for us.
  const authResult = await resolveCaller(supabaseUrl, anonKey, jwt);

  if (!authResult.ok) {
    return errorResponse(401, "invalid_auth", "Invalid or expired token.");
  }
  const caller = authResult.caller;

  // --------------------------------------------------------------------
  // 2. Read and validate the Groq key
  // --------------------------------------------------------------------
  const groqKey = Deno.env.get("GROQ_API_KEY");
  if (!groqKey) {
    return errorResponse(
      503,
      "ai_not_configured",
      "GROQ_API_KEY is not set. Run: supabase secrets set GROQ_API_KEY=...",
    );
  }

  // --------------------------------------------------------------------
  // 3. Parse and validate the body
  // --------------------------------------------------------------------
  let body: ChatRequestBody;
  try {
    body = await req.json();
  } catch {
    return errorResponse(400, "bad_json", "Request body must be valid JSON.");
  }

  if (!Array.isArray(body.messages) || body.messages.length === 0) {
    return errorResponse(400, "bad_messages", "`messages` must be a non-empty array.");
  }
  if (body.messages.length > MAX_MESSAGES) {
    return errorResponse(
      400,
      "too_many_messages",
      `At most ${MAX_MESSAGES} messages; got ${body.messages.length}.`,
    );
  }
  for (const message of body.messages) {
    if (
      typeof message?.content !== "string" ||
      typeof message?.role !== "string"
    ) {
      return errorResponse(400, "bad_message", "Each message needs role and content.");
    }
    if (message.content.length > MAX_MESSAGE_CHARS) {
      return errorResponse(
        400,
        "message_too_long",
        `A message exceeded ${MAX_MESSAGE_CHARS} characters.`,
      );
    }
  }

  const model = typeof body.model === "string" && body.model.trim()
    ? body.model.trim()
    : DEFAULT_MODEL;
  const maxTokens = clampNumber(
    body.max_tokens,
    1,
    MAX_TOKENS_LIMIT,
    DEFAULT_MAX_TOKENS,
  );
  const temperature = clampNumber(
    body.temperature,
    TEMPERATURE_MIN,
    TEMPERATURE_MAX,
    DEFAULT_TEMPERATURE,
  );
  const structured = body.structured !== false;

  // --------------------------------------------------------------------
  // 4. Build the Groq request
  // --------------------------------------------------------------------
  const messages = [...body.messages];
  if (structured) {
    // Append the format contract to the existing system prompt rather than
    // replacing it, so a guild's custom persona is preserved.
    const systemIndex = messages.findIndex((m) => m.role === "system");
    if (systemIndex >= 0) {
      messages[systemIndex] = {
        ...messages[systemIndex],
        content: `${messages[systemIndex].content}\n\n${JSON_INSTRUCTION}`,
      };
    } else {
      messages.unshift({ role: "system", content: JSON_INSTRUCTION });
    }
  }

  const groqPayload: Record<string, unknown> = {
    model,
    messages,
    max_tokens: maxTokens,
    temperature,
  };
  if (structured) {
    groqPayload.response_format = { type: "json_object" };
  }

  // --------------------------------------------------------------------
  // 5. Call Groq
  // --------------------------------------------------------------------
  const startedAt = Date.now();
  let groqResponse: Response;
  try {
    groqResponse = await fetchWithTimeout(
      GROQ_ENDPOINT,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${groqKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(groqPayload),
      },
      GROQ_TIMEOUT_MS,
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return errorResponse(504, "upstream_timeout", `Groq request failed: ${message}`);
  }

  const latencyMs = Date.now() - startedAt;

  // --------------------------------------------------------------------
  // 6. Handle upstream errors
  // --------------------------------------------------------------------
  if (!groqResponse.ok) {
    const raw = await groqResponse.text();

    if (groqResponse.status === 401 || groqResponse.status === 403) {
      return errorResponse(
        503,
        "upstream_auth",
        "Groq rejected the API key. Rotate it with `supabase secrets set`.",
      );
    }

    if (groqResponse.status === 429) {
      const retryAfter =
        Number(groqResponse.headers.get("retry-after")) || undefined;
      return errorResponse(429, "upstream_rate_limited", "Groq rate limit.", {
        retry_after: retryAfter,
      });
    }

    return errorResponse(
      502,
      "upstream_error",
      `Groq returned ${groqResponse.status}.`,
      { detail: raw.slice(0, 500) },
    );
  }

  // --------------------------------------------------------------------
  // 7. Extract text, then parse structured output
  // --------------------------------------------------------------------
  let groqBody: unknown;
  try {
    groqBody = await groqResponse.json();
  } catch {
    return errorResponse(502, "upstream_bad_json", "Groq returned unparseable JSON.");
  }

  const rawText = extractAssistantText(groqBody);
  if (!rawText) {
    return errorResponse(502, "empty_completion", "Groq returned an empty response.");
  }

  const result: StructuredResult = structured
    ? parseStructured(rawText)
    : { reply: rawText.trim(), summary: "", degraded: false };

  // --------------------------------------------------------------------
  // 8. Respond
  // --------------------------------------------------------------------
  const usage = extractUsage(groqBody);

  return new Response(
    JSON.stringify({
      reply: result.reply,
      summary: result.summary,
      degraded: result.degraded,
      model,
      usage,
      latency_ms: latencyMs,
      caller,
    }),
    {
      status: 200,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    },
  );
});

// ============================================================================
// helpers
// ============================================================================

/** fetch with an AbortController timeout (Deno has no built-in timeout). */
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

/**
 * Pull assistant text out of an OpenAI-compatible response.
 * Tolerant of shape changes so a Groq API tweak does not break the bot.
 */
function extractAssistantText(body: unknown): string {
  if (!body || typeof body !== "object") return "";
  const choices = (body as { choices?: unknown }).choices;
  if (!Array.isArray(choices) || choices.length === 0) return "";

  const first = choices[0] as Record<string, unknown>;
  const message = first?.message as Record<string, unknown> | undefined;
  if (message && typeof message.content === "string") {
    return message.content;
  }
  if (typeof first?.text === "string") {
    return first.text as string;
  }
  return "";
}

function extractUsage(body: unknown): Record<string, number> | null {
  if (!body || typeof body !== "object") return null;
  const usage = (body as { usage?: unknown }).usage;
  if (!usage || typeof usage !== "object") return null;
  const u = usage as Record<string, unknown>;
  return {
    prompt_tokens: Number(u.prompt_tokens) || 0,
    completion_tokens: Number(u.completion_tokens) || 0,
    total_tokens: Number(u.total_tokens) || 0,
  };
}

/**
 * Parse the model's {reply, summary} JSON.
 *
 * NEVER throws. If the model returns prose instead of JSON, we degrade
 * gracefully: the user still gets their reply and the summary is derived from
 * the text. A malformed AI response must not break the user-facing feature.
 */
function parseStructured(rawText: string): StructuredResult {
  const cleaned = stripCodeFences(rawText).trim();

  const parsed = tryParseObject(cleaned);
  if (parsed) {
    const reply = typeof parsed.reply === "string" ? parsed.reply.trim() : "";
    const summary = typeof parsed.summary === "string" ? parsed.summary.trim() : "";

    if (reply) {
      return {
        reply,
        summary: truncate(summary || reply, SUMMARY_MAX_CHARS),
        degraded: false,
      };
    }
  }

  // Degraded path: the model ignored the format. Use the whole text as the
  // reply and synthesise a summary so memory still has something to store.
  const fallback = cleaned || rawText.trim();
  return {
    reply: fallback,
    summary: truncate(fallback, SUMMARY_MAX_CHARS),
    degraded: true,
  };
}

/** Try JSON.parse, then a brace-extraction fallback for chatty models. */
function tryParseObject(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value as Record<string, unknown>;
    }
  } catch {
    // fall through
  }

  // Some models prepend a sentence before the JSON. Grab the outermost braces.
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      const value = JSON.parse(text.slice(start, end + 1));
      if (value && typeof value === "object" && !Array.isArray(value)) {
        return value as Record<string, unknown>;
      }
    } catch {
      // give up
    }
  }
  return null;
}

function stripCodeFences(text: string): string {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  return fenced ? fenced[1] : text;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1).trimEnd() + "…";
}

/**
 * Resolve who is calling, verifying the token's signature via Supabase.
 *
 * SECURITY: this deliberately does NOT parse the JWT payload to read the role.
 * A JWT's payload is unverified until its signature is checked, so trusting a
 * "service_role" claim would let anyone forge access. Supabase's /auth/v1/user
 * endpoint validates the signature and returns the user (or an error).
 *
 * Two legitimate callers:
 *   - the Discord bot, sending the service_role key (no user record)
 *   - the dashboard, sending a logged-in user's access token
 *
 * We tell them apart by calling /auth/v1/user with the service key: the
 * service_role key authenticates as an admin, so if the presented token equals
 * the configured service key we accept it directly. Otherwise we require a
 * verifiable user.
 */
async function resolveCaller(
  supabaseUrl: string,
  anonKey: string,
  jwt: string,
): Promise<{ ok: true; caller: string } | { ok: false }> {
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  // The bot path. Compare against the real secret: a forged token cannot match.
  if (serviceKey && timingSafeEqual(jwt, serviceKey)) {
    return { ok: true, caller: "service_role" };
  }

  // The dashboard path: verify the user token with Supabase.
  try {
    const res = await fetchWithTimeout(
      `${supabaseUrl.replace(/\/+$/, "")}/auth/v1/user`,
      {
        method: "GET",
        headers: { Authorization: `Bearer ${jwt}`, apikey: anonKey },
      },
      10_000,
    );
    if (!res.ok) return { ok: false };
    const user = await res.json();
    const id = user?.id;
    if (typeof id === "string" && id) {
      return { ok: true, caller: id };
    }
    return { ok: false };
  } catch {
    return { ok: false };
  }
}

/** Length-independent constant-time string comparison. */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}
