"""Groq AI service (via the Supabase `chat` Edge Function).

KEY PLACEMENT (decided 2026-10-20):
  GROQ_API_KEY lives in SUPABASE SECRETS, not in the bot env and not in the
  database. The bot calls the `chat` Edge Function, which reads the secret and
  proxies to Groq. Rationale: the bot-hosting.net free tier requires a manual
  renewal every 4 days; if the key were on the bot, missing a renewal would kill
  AI chat outright. Via the Edge Function the dashboard path keeps working, the
  key never lands in a DB backup or the Realtime stream, and it is rotated in
  one place.

This module is therefore an HTTP client, not a Groq SDK client. The Groq API is
OpenAI-compatible, but we never talk to it directly.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import settings
from utils.logging_setup import get_logger

log = get_logger("services.groq")

# --- tunables ---------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 45.0
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5

# How many messages total are retained per channel before trimming. The guild's
# ai_context_messages controls how many are *sent*; this caps how many we *keep*
# in memory so a busy channel cannot grow the deque without bound.
MAX_RETAINED_MESSAGES = 50


class GroqError(RuntimeError):
    """Any failure while talking to the AI backend."""


class GroqRateLimited(GroqError):
    """The backend (or Groq) returned a rate-limit response."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GroqUnavailable(GroqError):
    """The Edge Function or Groq is unreachable / misconfigured."""


@dataclass
class ChatMessage:
    role: str  # 'user' | 'assistant' | 'system'
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class _ChannelState:
    """Per-channel conversation history and in-flight request tracking."""

    history: deque[ChatMessage] = field(
        default_factory=lambda: deque(maxlen=MAX_RETAINED_MESSAGES)
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class GroqService:
    """Talks to the AI backend and owns per-channel conversation state.

    Two entry points, deliberately kept separate so they cannot be confused:
      - :meth:`respond_to_mention`   the bot was @mentioned
      - :meth:`respond_to_command`   the /ask slash command
    """

    def __init__(self, *, supabase_url: str, service_key: str) -> None:
        self._endpoint = f"{supabase_url.rstrip('/')}/functions/v1/chat"
        self._service_key = service_key
        self._channels: dict[int, _ChannelState] = {}
        # (guild_id, user_id) -> monotonic timestamp of last AI reply
        self._user_cooldowns: dict[tuple[int, int], float] = {}
        # guild_id -> monotonic timestamp of last AI reply (rolling guild cap)
        self._guild_last_call: dict[int, float] = {}

    # --- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Release per-channel state. Called on graceful shutdown."""
        self._channels.clear()
        self._user_cooldowns.clear()
        self._guild_last_call.clear()

    # --- public API --------------------------------------------------------

    async def respond_to_mention(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        user_name: str,
        prompt: str,
        context_messages: int,
        system_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        cooldown_seconds: int,
    ) -> str:
        """Generate a reply to a message that mentioned the bot."""
        self._enforce_cooldown(guild_id, user_id, cooldown_seconds)
        return await self._complete(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            prompt=prompt,
            context_messages=context_messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def respond_to_command(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        user_name: str,
        prompt: str,
        context_messages: int,
        system_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        cooldown_seconds: int,
    ) -> str:
        """Generate a reply to /ask."""
        self._enforce_cooldown(guild_id, user_id, cooldown_seconds)
        return await self._complete(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            prompt=prompt,
            context_messages=context_messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def remaining_cooldown(self, guild_id: int, user_id: int, cooldown_seconds: int) -> float:
        """Seconds until this user may talk to the AI again (0 when ready)."""
        if cooldown_seconds <= 0:
            return 0.0
        loop = asyncio.get_event_loop()
        last = self._user_cooldowns.get((guild_id, user_id))
        if last is None:
            return 0.0
        elapsed = loop.time() - last
        return max(0.0, cooldown_seconds - elapsed)

    def reset_channel(self, channel_id: int) -> None:
        """Forget conversation history for a channel (used by /ask reset)."""
        state = self._channels.get(channel_id)
        if state is not None:
            state.history.clear()

    def history_length(self, channel_id: int) -> int:
        state = self._channels.get(channel_id)
        return len(state.history) if state else 0

    # --- internals ---------------------------------------------------------

    def _state(self, channel_id: int) -> _ChannelState:
        state = self._channels.get(channel_id)
        if state is None:
            state = _ChannelState()
            self._channels[channel_id] = state
        return state

    def _enforce_cooldown(self, guild_id: int, user_id: int, cooldown_seconds: int) -> None:
        remaining = self.remaining_cooldown(guild_id, user_id, cooldown_seconds)
        if remaining > 0:
            raise GroqRateLimited(
                f"Slow down - try again in {remaining:.1f}s",
                retry_after=remaining,
            )

    async def _complete(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        user_name: str,
        prompt: str,
        context_messages: int,
        system_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        max_retries: int = MAX_RETRIES,
    ) -> str:
        """Call the Edge Function, retrying transient failures.

        One in-flight request per channel is enforced with a lock so two fast
        messages cannot interleave into a single mangled reply.
        """
        if not self._service_key or not self._endpoint.startswith("http"):
            raise GroqUnavailable("AI backend is not configured (missing Supabase credentials)")

        state = self._state(channel_id)

        async with state.lock:
            # Build the outgoing message list: system + trimmed history + prompt
            trimmed_history = list(state.history)[-max(0, context_messages):]
            messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
            messages.extend(m.as_dict() for m in trimmed_history)
            messages.append({"role": "user", "content": f"{user_name}: {prompt}"})

            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

            headers = {
                "Authorization": f"Bearer {self._service_key}",
                "Content-Type": "application/json",
            }

            last_error: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                        response = await client.post(self._endpoint, json=payload, headers=headers)

                    if response.status_code == 429:
                        retry_after = _parse_retry_after(response)
                        raise GroqRateLimited(
                            "The AI is being rate limited right now.",
                            retry_after=retry_after,
                        )

                    if response.status_code == 401:
                        raise GroqUnavailable(
                            "AI backend rejected the credentials. Check the chat function's secret."
                        )

                    if response.status_code >= 500:
                        raise GroqUnavailable(
                            f"AI backend error {response.status_code}"
                        )

                    response.raise_for_status()
                    text = self._extract_text(response.json())
                    if not text:
                        raise GroqError("The AI returned an empty response.")

                    # Record the exchange only on success.
                    state.history.append(ChatMessage("user", f"{user_name}: {prompt}"))
                    state.history.append(ChatMessage("assistant", text))

                    loop = asyncio.get_event_loop()
                    self._user_cooldowns[(guild_id, user_id)] = loop.time()
                    return text

                except GroqRateLimited:
                    raise
                except GroqUnavailable as exc:
                    last_error = exc
                    # Retry only server-side blips, not auth/config problems.
                    if "rejected the credentials" in str(exc) or "not configured" in str(exc):
                        raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = GroqUnavailable(f"AI backend unreachable: {exc}")
                except httpx.HTTPStatusError as exc:
                    last_error = GroqError(
                        f"AI backend returned {exc.response.status_code}"
                    )
                except Exception as exc:  # noqa: BLE001 - never let this crash a handler
                    last_error = GroqError(f"Unexpected AI failure: {exc}")

                if attempt < max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

            log.error("AI request failed after retries: %s", last_error, exc_info=last_error)
            raise last_error or GroqError("AI request failed")

    @staticmethod
    def _extract_text(body: Any) -> str:
        """Pull the assistant text out of an OpenAI-compatible response.

        Tolerates the common shapes so a small change in the Edge Function's
        response envelope does not break the bot.
        """
        if not isinstance(body, dict):
            return ""
        # Direct passthrough of the Groq response
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"].strip()
                if isinstance(first.get("text"), str):
                    return first["text"].strip()
        # Simplified envelope: { "reply": "..." }
        for key in ("reply", "content", "text", "output"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def build_service() -> GroqService:
    """Construct the service from application settings."""
    return GroqService(
        supabase_url=settings.supabase_url,
        service_key=settings.supabase_service_key,
    )


__all__ = [
    "GroqService",
    "GroqError",
    "GroqRateLimited",
    "GroqUnavailable",
    "ChatMessage",
    "build_service",
]
