"""Gateway listeners and background loops for activity + moderation tracking.

This cog is deliberately thin: the counters live in
``services/activity_tracker.py`` and the audit-log/Dyno logic in
``services/moderation_tracker.py``. Here we only translate Discord events into
calls on those services, and own the two background loops.

Why a separate cog instead of adding listeners to general_cog?
    general_cog is about commands and membership bookkeeping. Activity tracking
    can be disabled entirely (``ACTIVITY_TRACKING_ENABLED=false``), and keeping
    it in one optional cog makes that switch honest: no tracker, no listeners,
    no loop, no database writes.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import discord
from discord.ext import commands

from config import settings
from db import supabase_client
from services.activity_tracker import ActivityTracker
from services.config_service import ConfigService
from services.moderation_tracker import ModerationTracker
from utils.logging_setup import get_logger

log = get_logger("cogs.activity")


class ActivityCog(commands.Cog, name="Activity"):
    """Counts messages, voice, joins, boosts and moderation actions."""

    def __init__(self, bot: commands.Bot, config_service: ConfigService) -> None:
        self.bot = bot
        self.config = config_service
        self.tracker: ActivityTracker | None = None
        self.moderation: ModerationTracker | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

        if settings.activity_tracking_enabled:
            self.tracker = ActivityTracker(
                config_service, retention_days=settings.report_retention_days
            )
            self.moderation = ModerationTracker(
                bot,
                config_service,
                poll_seconds=settings.moderation_poll_seconds,
                backfill_days=settings.moderation_backfill_days,
            )
        else:
            log.warning(
                "Activity tracking is disabled; monthly reports will have no "
                "activity or moderation numbers"
            )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        if self.tracker is None:
            return
        if self.moderation is not None:
            self.moderation.start()
        self._stopping.clear()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="activity-flush")

    async def cog_unload(self) -> None:
        await self.shutdown()

    async def shutdown(self) -> None:
        """Stop the loops and make one last best-effort flush."""
        self._stopping.set()
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        if self.moderation is not None:
            with contextlib.suppress(Exception):
                await self.moderation.stop()
        if self.tracker is not None:
            await self._flush_all(final=True)
        log.info("Activity tracking stopped")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.tracker is None:
            return
        for guild in self.bot.guilds:
            try:
                self._seed_voice(guild)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not seed voice states for guild %s: %s", guild.id, exc)
            try:
                await self._sync_events(guild)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not sync scheduled events for guild %s: %s", guild.id, exc)

    # ------------------------------------------------------------------
    # flush loop
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        with contextlib.suppress(Exception):
            await self.bot.wait_until_ready()
        interval = max(60, int(settings.activity_flush_seconds))
        log.info("Activity flush loop started (every %ds)", interval)

        while not self._stopping.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            if self._stopping.is_set():
                break
            await self._flush_all()

    async def _flush_all(self, *, final: bool = False) -> None:
        if self.tracker is None:
            return
        for guild in list(self.bot.guilds):
            online = self._online_count(guild)
            try:
                await asyncio.to_thread(
                    self.tracker.flush_guild, guild, online_count=online
                )
            except Exception as exc:  # noqa: BLE001 - one guild must not stop the rest
                log.warning("Activity flush failed for guild %s: %s", guild.id, exc)

        # Prune at most once a day. Skip on the final flush: shutdown should be
        # quick and pruning is not urgent.
        if not final:
            try:
                await asyncio.to_thread(self.tracker.prune)
            except Exception as exc:  # noqa: BLE001
                log.warning("Activity prune failed: %s", exc)

    def _online_count(self, guild: discord.Guild) -> int | None:
        """Online members, or None when the presence intent is off.

        With the intent off every member reports as offline, so 0 would be a
        lie. NULL is stored and the report simply omits online counts.
        """
        if not self.bot.intents.presences:
            return None
        return sum(
            1 for member in guild.members if member.status is not discord.Status.offline
        )

    def _seed_voice(self, guild: discord.Guild) -> None:
        assert self.tracker is not None
        states = {
            member.id: state
            for member in guild.members
            if not member.bot
            and (state := member.voice) is not None
            and state.channel is not None
        }
        self.tracker.seed_voice_states(guild.id, states)

    # ------------------------------------------------------------------
    # activity listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        # Bot messages are not activity, but the Dyno mod-log channel is. The
        # moderation tracker does the channel/author checks itself.
        if message.author.bot:
            if self.moderation is not None:
                try:
                    await self.moderation.handle_modlog_message(message)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Dyno mod-log handling failed: %s", exc)
            return

        if self.tracker is None:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        channel = message.channel
        self.tracker.record_message(
            message.guild.id,
            channel_id=getattr(channel, "id", None),
            channel_name=getattr(channel, "name", None),
            user_id=message.author.id,
            user_name=getattr(message.author, "display_name", None) or str(message.author),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if self.tracker is None:
            return
        self.tracker.record_join(
            member.guild.id,
            user_id=member.id,
            user_name=str(member),
            is_bot=member.bot,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if self.tracker is None:
            return
        self.tracker.record_leave(
            member.guild.id,
            user_id=member.id,
            user_name=str(member),
            is_bot=member.bot,
        )

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.bot or after.bot:
            return

        # Boosts. premium_since is set when a member is actively boosting and
        # cleared when they stop, so the transition is the event.
        if self.tracker is not None:
            was_boosting = before.premium_since is not None
            is_boosting = after.premium_since is not None
            if not was_boosting and is_boosting:
                self.tracker.record_boost(
                    after.guild.id,
                    user_id=after.id,
                    user_name=str(after),
                    kind="start",
                )
            elif was_boosting and not is_boosting:
                self.tracker.record_boost(
                    after.guild.id,
                    user_id=after.id,
                    user_name=str(after),
                    kind="stop",
                )

        # Timeouts. discord.py exposes Member.timed_out_until; the transition
        # from None to a datetime is the action. Extensions of an existing
        # timeout are deliberately not counted again.
        if self.moderation is not None:
            before_until = getattr(before, "timed_out_until", None)
            after_until = getattr(after, "timed_out_until", None)
            if before_until is None and after_until is not None:
                try:
                    await self.moderation.record_timeout(after, after_until)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not record timeout for %s: %s", after.id, exc)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.tracker is None or member.bot:
            return
        self.tracker.update_voice_state(
            member.guild.id,
            user_id=member.id,
            user_name=str(member),
            before_channel=before.channel,
            after_channel=after.channel,
        )

    # ------------------------------------------------------------------
    # scheduled events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        await self._upsert_event(event)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, event: discord.ScheduledEvent) -> None:
        await self._upsert_event(event)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        try:
            await asyncio.to_thread(
                supabase_client.delete,
                "guild_events",
                filters={"guild_id": event.guild.id, "event_id": event.id},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not delete scheduled event %s: %s", event.id, exc)

    async def _upsert_event(self, event: discord.ScheduledEvent) -> None:
        try:
            await asyncio.to_thread(
                supabase_client.upsert,
                "guild_events",
                self._event_row(event),
                on_conflict="guild_id,event_id",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not store scheduled event %s: %s", event.id, exc)

    async def _sync_events(self, guild: discord.Guild) -> None:
        """Mirror every scheduled event and drop rows for deleted ones."""
        rows = [self._event_row(event) for event in guild.scheduled_events]
        if rows:
            await asyncio.to_thread(
                supabase_client.upsert,
                "guild_events",
                rows,
                on_conflict="guild_id,event_id",
            )

        current = {event.id for event in guild.scheduled_events}
        stored = await asyncio.to_thread(
            supabase_client.select,
            "guild_events",
            "event_id",
            filters={"guild_id": guild.id},
        )
        for row in stored:
            event_id = row.get("event_id")
            if event_id is not None and int(event_id) not in current:
                await asyncio.to_thread(
                    supabase_client.delete,
                    "guild_events",
                    filters={"guild_id": guild.id, "event_id": int(event_id)},
                )

    @staticmethod
    def _event_row(event: discord.ScheduledEvent) -> dict[str, Any]:
        status = getattr(event.status, "name", None)
        return {
            "guild_id": event.guild.id,
            "event_id": event.id,
            "name": event.name,
            "description": event.description,
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
            "status": status.lower() if status else None,
            "user_count": event.user_count,
            "subscriber_count": event.subscriber_count,
            "channel_id": getattr(event.channel, "id", None),
        }


async def setup(bot: commands.Bot) -> None:
    cog = ActivityCog(bot, bot.config_service)  # type: ignore[attr-defined]
    await bot.add_cog(cog)
