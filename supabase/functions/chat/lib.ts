// ============================================================================
// Shared helpers for the `chat` Edge Function.
// ============================================================================

export interface ChatRequestBody {
  /** Groq model id, e.g. "llama-3.3-70b-versatile". */
  model?: string;
  /** OpenAI-compatible message array: [{role, content}, ...]. */
  messages: Array<{ role: "system" | "user" | "assistant"; content: string }>;
  max_tokens?: number;
  temperature?: number;
  /** When true, ask the model for {reply, summary} JSON. Default true. */
  structured?: boolean;
}

export interface StructuredResult {
  /** What the user sees. */
  reply: string;
  /** One-line gist stored as memory. May be empty if the model misbehaved. */
  summary: string;
  /** True when the model returned unparseable output and we fell back. */
  degraded: boolean;
}

export const DEFAULT_MODEL = "llama-3.3-70b-versatile";
export const GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions";

// Hard bounds so a misconfigured guild cannot burn the whole quota in one call.
export const MAX_TOKENS_LIMIT = 2048;
export const DEFAULT_MAX_TOKENS = 512;
export const TEMPERATURE_MIN = 0;
export const TEMPERATURE_MAX = 2;
export const DEFAULT_TEMPERATURE = 0.7;
export const MAX_MESSAGES = 40;
export const MAX_MESSAGE_CHARS = 6000;
export const SUMMARY_MAX_CHARS = 500;

/**
 * The instruction that makes structured output work.
 *
 * We use Groq's JSON mode (response_format) rather than asking for a delimiter
 * syntax. Prompt-based separators eventually break: a chatty model forgets the
 * marker, or uses it inside the reply, and the stored memory is then garbage.
 */
export const JSON_INSTRUCTION = [
  "You must reply with a single JSON object and nothing else.",
  'It must have exactly two string keys: "reply" and "summary".',
  '"reply" is your complete message to the user, in character.',
  `"summary" is a short factual one-liner (max ${SUMMARY_MAX_CHARS} characters) describing what the user asked and what you said, written for your own future memory. Write it in third person, e.g. "User asked about X; I explained Y."`,
  "Do not wrap the JSON in markdown fences. Do not add commentary outside the JSON.",
].join(" ");

/** Clamp a number into [min, max], returning fallback for non-numbers. */
export function clampNumber(
  value: unknown,
  min: number,
  max: number,
  fallback: number,
): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

/** Structured error payload the bot understands. */
export function errorResponse(
  status: number,
  code: string,
  message: string,
  extra: Record<string, unknown> = {},
): Response {
  return new Response(
    JSON.stringify({ error: { code, message, ...extra } }),
    { status, headers: { "Content-Type": "application/json" } },
  );
}
