"""Server event logging: Discord log channel + dashboard feed.

Two destinations, one call site:
  1. an embed in the guild's configured log channel
  2. a row in ``bot_events`` for the dashboard activity feed

Design rules that matter:
  - **Fail-silent.** If the log channel is deleted or the bot lacks permission,
    logging must NOT raise. A logging failure turning into an error storm is
    worse than no logs at all. Failures are reported once, then the guild's
    logging is disabled in memory for the session.
  - **Re-entrancy guard.** If an error is logged while already logging, we must
    not recurse.
  - **Category gating.** Only categories in ``log_categories`` are delivered.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

import discord

from db import config_cache, supabase_client
from utils.logging_setup import get_logger

log = get_logger("utils.event_log")

VALID_CATEGORIES = {"config", "commands", "qotd", "errors", "members", "ai"}

LEVEL_COLOURS = {
    "info": discord.Colour.blurple(),
    "warning": discord.Colour.orange(),
    "error": discord.Colour.red(),
}

CATEGORY_ICONS = {
    "config": "⚙️",
    "commands": "⌨️",
    "qotd": "❓",
    "errors": "⚠️",
    "members": "👋",
    "ai": "🤖",
}


class EventLogger:
    """Emits notable events to a guild's log channel and the dashboard feed."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        # guild_id -> True once we have given up on that guild's log channel
        self._disabled: set[int] = set()
        self._in_flight = False

    # ------------------------------------------------------------------

    async def emit(
        self,
        guild_id: int | None,
        category: str,
        *,
        title: str,
        description: str | None = None,
        level: str = "info",
        actor_id: int | None = None,
        actor_name: str | None = None,
        channel_id: int | None = None,
        fields: Iterable[tuple[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
        to_discord: bool = True,
        to_database: bool = True,
    ) -> None:
        """Record an event. Never raises."""
        if guild_id is None:
            return
        if category not in VALID_CATEGORIES:
            log.warning("Unknown event category %r; sending as 'errors'", category)
            category = "errors"
        if level not in LEVEL_COLOURS:
            level = "info"

        # Re-entrancy guard: an exception inside logging must not recurse.
        if self._in_flight:
            log.debug("Suppressed nested log event: %s", title)
            return
        self._in_flight = True

        try:
            config = config_cache.get(guild_id)

            if category not in config.log_categories:
                # Category not selected: skip Discord, but still record to the DB
                # so the dashboard feed stays complete.
                to_discord = False

            if to_database:
                await self._write_row(
                    guild_id,
                    category,
                    title=title,
                    description=description,
                    level=level,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    channel_id=channel_id,
                    metadata=metadata,
                )

            if to_discord:
                await self._send_embed(
                    guild_id,
                    config,
                    category,
                    title=title,
                    description=description,
                    level=level,
                    actor_name=actor_name,
                    fields=fields,
                )
        except Exception as exc:  # noqa: BLE001 - logging must never escalate
            log.error("Event logging failed: %s", exc, exc_info=True)
        finally:
            self._in_flight = False

    # ------------------------------------------------------------------

    async def _write_row(
        self,
        guild_id: int,
        category: str,
        *,
        title: str,
        description: str | None,
        level: str,
        actor_id: int | None,
        actor_name: str | None,
        channel_id: int | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        payload = {
            "guild_id": guild_id,
            "category": category,
            "title": title[:200],
            "description": (description or None) and description[:1500],
            "actor_id": actor_id,
            "actor_name": actor_name,
            "channel_id": channel_id,
            "level": level,
            "metadata": metadata or {},
        }
        try:
            await asyncio.to_thread(supabase_client.upsert, "bot_events", payload)
        except Exception as exc:
            # The dashboard feed is nice-to-have; never let it break a command.
            log.debug("Could not write bot_events row: %s", exc)

    async def _send_embed(
        self,
        guild_id: int,
        config: Any,
        category: str,
        *,
        title: str,
        description: str | None,
        level: str,
        actor_name: str | None,
        fields: Iterable[tuple[str, str]] | None,
    ) -> None:
        if not config.log_enabled:
            return
        if guild_id in self._disabled:
            return

        channel_id = config.log_channel_id
        if channel_id is None:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            self._disable(guild_id, "log channel no longer exists")
            return

        icon = CATEGORY_ICONS.get(category, "•")
        embed = discord.Embed(
            title=f"{icon} {title}",
            description=description,
            colour=LEVEL_COLOURS[level],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"category: {category}")
        if actor_name:
            embed.add_field(name="By", value=actor_name, inline=True)
        for name, value in (fields or []):
            embed.add_field(name=name, value=value, inline=True)

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            self._disable(guild_id, "missing Send Messages / Embed Links permission")
        except discord.NotFound:
            self._disable(guild_id, "log channel was deleted")
        except discord.HTTPException as exc:
            log.warning("Log embed failed for guild %s: %s", guild_id, exc)

    def _disable(self, guild_id: int, reason: str) -> None:
        """Stop trying to log to this guild for the rest of the session."""
        if guild_id in self._disabled:
            return
        self._disabled.add(guild_id)
        log.warning(
            "Disabling Discord logging for guild %s for this session: %s",
            guild_id,
            reason,
        )

    def reset(self, guild_id: int | None = None) -> None:
        """Re-enable logging (call after an admin fixes the channel)."""
        if guild_id is None:
            self._disabled.clear()
        else:
            self._disabled.discard(guild_id)


def make_emitter(bot: Any):
    """Return a bound ``emit`` callable, stored on the bot as ``bot.emit_log``."""
    logger = EventLogger(bot)
    bot.event_logger = logger
    return logger.emit


__all__ = ["EventLogger", "make_emitter", "VALID_CATEGORIES"]
